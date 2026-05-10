"""LLM Proxy subprocess manager with health checks and auto-restart.

Manages the OpenAI-compatible proxy (e.g. `npx openai-oauth`) as a child
process, monitors its health via periodic HTTP pings, and restarts it
automatically when it crashes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)


class ProxyState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    RESTARTING = "restarting"
    COOLDOWN = "cooldown"
    AUTH_REQUIRED = "auth_required"


class PingResult(StrEnum):
    HEALTHY = "healthy"
    DEAD = "dead"
    AUTH_EXPIRED = "auth_expired"


@dataclass
class ProxyConfig:
    """Configuration for the LLM proxy subprocess."""

    enabled: bool = True
    command: str = "npx openai-oauth"
    port: int = 10531
    auto_restart: bool = True
    health_check_interval: int = 30  # seconds
    max_restarts: int = 5
    restart_window: int = 600  # seconds — reset restart counter after this
    startup_timeout: int = 30  # max seconds to wait for proxy to become ready
    shutdown_timeout: int = 5  # seconds before SIGKILL


class LLMProxyManager:
    """Manages an LLM proxy as an asyncio subprocess.

    Lifecycle:
        start() → _run_health_loop() → stop()

    The manager captures stdout/stderr into a ring buffer, performs periodic
    health checks, and auto-restarts the proxy on failure.
    """

    def __init__(self, config: ProxyConfig, tg_notify=None):
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._state = ProxyState.STOPPED
        self._health_task: asyncio.Task | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._log_buffer: deque[str] = deque(maxlen=100)
        self._restart_times: deque[float] = deque(maxlen=config.max_restarts)
        self._total_restarts = 0
        self._started_at: float | None = None
        self._last_health_check: float | None = None
        self._last_healthy_at: float | None = None
        self._went_unhealthy_at: float | None = None
        self._tg_notify = tg_notify  # async callable(text) for Telegram alerts
        self._stopping = False

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def state(self) -> ProxyState:
        return self._state

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def uptime_seconds(self) -> float:
        if self._started_at is None:
            return 0
        return time.monotonic() - self._started_at

    @property
    def log_lines(self) -> list[str]:
        return list(self._log_buffer)

    def health_info(self) -> dict:
        """Return health dict for /health endpoint."""
        return {
            "status": self._state.value,
            "pid": self.pid,
            "uptime_seconds": round(self.uptime_seconds),
            "restarts": self._total_restarts,
            "last_check": self._last_health_check,
            "port": self.config.port,
        }

    async def start(self) -> bool:
        """Start the proxy subprocess and wait for it to become healthy.

        Returns True if proxy became healthy within startup_timeout.
        """
        if not self.config.enabled:
            logger.info("LLM Proxy disabled in config")
            return False

        self._stopping = False
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
        )
        ok = await self._spawn()
        if not ok:
            return False

        # Wait for first successful health check
        healthy = await self._wait_for_ready(self.config.startup_timeout)
        if healthy:
            self._state = ProxyState.HEALTHY
            self._last_healthy_at = time.monotonic()
            logger.info(f"LLM Proxy healthy (PID {self.pid}, port {self.config.port})")
        else:
            self._state = ProxyState.UNHEALTHY
            logger.warning(
                f"LLM Proxy started (PID {self.pid}) but not yet healthy "
                f"after {self.config.startup_timeout}s — will keep checking"
            )

        # Start background health monitoring
        self._health_task = asyncio.create_task(self._health_loop())
        return healthy

    async def stop(self):
        """Gracefully stop the proxy and all monitoring tasks."""
        self._stopping = True
        self._state = ProxyState.STOPPED

        # Cancel health loop
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Kill process
        await self._kill_process()

        # Close HTTP session
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        logger.info("LLM Proxy stopped")

    # ── Subprocess management ───────────────────────────────────────────

    async def _spawn(self) -> bool:
        """Spawn the proxy subprocess."""
        self._state = ProxyState.STARTING

        cmd_parts = self.config.command.split()
        executable = cmd_parts[0]
        args = cmd_parts[1:]

        # Resolve executable path (npx via nvm needs full path)
        resolved = self._resolve_executable(executable)
        if not resolved:
            logger.error(
                f"LLM Proxy executable not found: {executable}. "
                "Checked PATH, nvm, and common locations."
            )
            self._state = ProxyState.STOPPED
            return False

        env = self._build_env()

        try:
            self._process = await asyncio.create_subprocess_exec(
                resolved,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._started_at = time.monotonic()
            self._log_buffer.append(
                f"[proxy] Started: {resolved} {' '.join(args)} (PID {self._process.pid})"
            )
            logger.info(f"LLM Proxy spawned: PID {self._process.pid}")

            # Start stdout/stderr readers
            self._stdout_task = asyncio.create_task(
                self._read_stream(self._process.stdout, "stdout")
            )
            self._stderr_task = asyncio.create_task(
                self._read_stream(self._process.stderr, "stderr")
            )
            return True

        except Exception as e:
            logger.error(f"Failed to spawn LLM Proxy: {e}")
            self._state = ProxyState.STOPPED
            return False

    def _resolve_executable(self, name: str) -> str | None:
        """Find the executable, checking nvm paths if needed."""
        # 1. shutil.which with current PATH
        found = shutil.which(name)
        if found:
            return found

        # 2. Check nvm installation
        nvm_dir = os.environ.get("NVM_DIR", os.path.expanduser("~/.nvm"))
        nvm_versions = Path(nvm_dir) / "versions" / "node"
        if nvm_versions.exists():
            # Pick the latest version
            versions = sorted(nvm_versions.iterdir(), reverse=True)
            for v in versions:
                candidate = v / "bin" / name
                if candidate.exists():
                    return str(candidate)

        # 3. Common paths
        for prefix in [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.expanduser("~/.local/bin"),
        ]:
            candidate = Path(prefix) / name
            if candidate.exists():
                return str(candidate)

        return None

    def _build_env(self) -> dict[str, str]:
        """Build environment for the subprocess, inheriting current env."""
        env = os.environ.copy()

        # Ensure nvm node is on PATH
        nvm_dir = env.get("NVM_DIR", os.path.expanduser("~/.nvm"))
        nvm_versions = Path(nvm_dir) / "versions" / "node"
        if nvm_versions.exists():
            versions = sorted(nvm_versions.iterdir(), reverse=True)
            if versions:
                node_bin = str(versions[0] / "bin")
                env["PATH"] = f"{node_bin}:{env.get('PATH', '')}"

        return env

    async def _kill_process(self):
        """Send SIGTERM, wait, then SIGKILL if needed."""
        if not self._process:
            return

        # Cancel stream readers
        for task in (self._stdout_task, self._stderr_task):
            if task and not task.done():
                task.cancel()

        pid = self._process.pid
        try:
            if self._process.returncode is None:
                self._process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(
                        self._process.wait(),
                        timeout=self.config.shutdown_timeout,
                    )
                except TimeoutError:
                    logger.warning(
                        f"LLM Proxy PID {pid} didn't stop in "
                        f"{self.config.shutdown_timeout}s, sending SIGKILL"
                    )
                    self._process.kill()
                    await self._process.wait()
        except ProcessLookupError:
            pass  # Already dead
        except Exception as e:
            logger.debug(f"Error killing proxy: {e}")

        self._process = None
        self._log_buffer.append(f"[proxy] Process {pid} terminated")

    # ── Health monitoring ───────────────────────────────────────────────

    async def _health_loop(self):
        """Periodically check proxy health and restart if needed."""
        consecutive_failures = 0
        auth_notified = False  # Only notify about expired token once

        while not self._stopping:
            try:
                await asyncio.sleep(self.config.health_check_interval)

                if self._stopping:
                    break

                # Check if process is still alive
                if self._process and self._process.returncode is not None:
                    exit_code = self._process.returncode
                    logger.warning(f"LLM Proxy exited with code {exit_code}")
                    self._log_buffer.append(f"[proxy] Process exited: code {exit_code}")
                    consecutive_failures = 3  # Skip straight to restart
                    auth_notified = False
                else:
                    # HTTP health check
                    ping_result = await self._ping()
                    self._last_health_check = time.monotonic()

                    if ping_result == PingResult.HEALTHY:
                        if self._state != ProxyState.HEALTHY:
                            offline_secs = 0
                            if self._went_unhealthy_at:
                                offline_secs = round(time.monotonic() - self._went_unhealthy_at)
                            was_auth = self._state == ProxyState.AUTH_REQUIRED
                            logger.info(
                                "LLM Proxy recovered"
                                + (f" (was offline {offline_secs}s)" if offline_secs else "")
                            )
                            msg = "✅ LLM Proxy восстановлен"
                            if was_auth:
                                msg += " (токен обновлён)"
                            elif offline_secs:
                                msg += f" (был offline {offline_secs} сек)"
                            await self._notify(msg)
                            self._went_unhealthy_at = None
                            auth_notified = False
                        self._state = ProxyState.HEALTHY
                        self._last_healthy_at = time.monotonic()
                        consecutive_failures = 0
                        continue

                    # Auth expired — don't restart, just notify
                    if ping_result == PingResult.AUTH_EXPIRED:
                        if self._state != ProxyState.AUTH_REQUIRED:
                            self._went_unhealthy_at = self._went_unhealthy_at or time.monotonic()
                        self._state = ProxyState.AUTH_REQUIRED
                        consecutive_failures = 0  # Don't trigger restart
                        if not auth_notified:
                            auth_notified = True
                            logger.error("LLM Proxy: OAuth token expired — re-login required")
                            await self._notify(
                                "🔑 LLM Proxy: OAuth токен истёк!\n\n"
                                "Выполни на сервере:\n"
                                "npx @openai/codex login\n\n"
                                "После этого proxy автоматически "
                                "подхватит новый токен."
                            )
                        continue

                    consecutive_failures += 1
                    logger.debug(f"LLM Proxy health check failed ({consecutive_failures}/3)")

                # Need restart?
                if consecutive_failures >= 3 and self.config.auto_restart:
                    if self._state == ProxyState.HEALTHY:
                        self._went_unhealthy_at = time.monotonic()
                    self._state = ProxyState.UNHEALTHY

                    if self._can_restart():
                        await self._do_restart()
                        consecutive_failures = 0
                    else:
                        self._state = ProxyState.COOLDOWN
                        logger.error(
                            f"LLM Proxy: too many restarts "
                            f"({self.config.max_restarts} in "
                            f"{self.config.restart_window}s) — cooldown"
                        )
                        await self._notify(
                            f"🔴 LLM Proxy: слишком много перезапусков "
                            f"({self.config.max_restarts}), ожидание cooldown"
                        )
                        # Wait for the restart window to expire
                        await asyncio.sleep(self.config.restart_window)
                        self._restart_times.clear()
                        logger.info("LLM Proxy cooldown expired, will retry")

                elif consecutive_failures >= 3:
                    if self._state != ProxyState.UNHEALTHY:
                        self._went_unhealthy_at = time.monotonic()
                    self._state = ProxyState.UNHEALTHY

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"LLM Proxy health loop error: {e}")
                await asyncio.sleep(10)

    async def _ping(self) -> PingResult:
        """HTTP GET /v1/models to check if proxy is alive."""
        url = f"http://127.0.0.1:{self.config.port}/v1/models"
        try:
            session = self._http_session or aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
            )
            async with session.get(url) as resp:
                if resp.status == 200:
                    return PingResult.HEALTHY
                # Check for auth-related errors in response body
                try:
                    body = await resp.json()
                    error_msg = str(body.get("error", {}).get("message", "")).lower()
                    if any(
                        marker in error_msg
                        for marker in (
                            "expired",
                            "signing in again",
                            "unauthorized",
                            "authentication",
                            "auth",
                        )
                    ):
                        return PingResult.AUTH_EXPIRED
                except Exception:
                    pass
                return PingResult.DEAD
        except Exception:
            return PingResult.DEAD

    async def _wait_for_ready(self, timeout: float) -> bool:
        """Poll until proxy responds or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (await self._ping()) == PingResult.HEALTHY:
                return True
            await asyncio.sleep(1)
        return False

    def _can_restart(self) -> bool:
        """Check if we haven't exceeded max_restarts in restart_window."""
        now = time.monotonic()
        # Purge old restart times outside window
        while self._restart_times and (now - self._restart_times[0] > self.config.restart_window):
            self._restart_times.popleft()
        return len(self._restart_times) < self.config.max_restarts

    async def _do_restart(self):
        """Kill current process and spawn a new one."""
        self._total_restarts += 1
        self._restart_times.append(time.monotonic())
        self._state = ProxyState.RESTARTING

        logger.warning(f"LLM Proxy restarting (attempt #{self._total_restarts})")
        await self._notify(f"⚠️ LLM Proxy упал! Перезапуск #{self._total_restarts}...")

        await self._kill_process()
        await asyncio.sleep(2)  # Brief pause before restart

        ok = await self._spawn()
        if ok:
            healthy = await self._wait_for_ready(self.config.startup_timeout)
            if healthy:
                self._state = ProxyState.HEALTHY
                self._last_healthy_at = time.monotonic()
            else:
                self._state = ProxyState.UNHEALTHY
        else:
            self._state = ProxyState.STOPPED

    # ── Stream readers ──────────────────────────────────────────────────

    async def _read_stream(self, stream: asyncio.StreamReader | None, label: str):
        """Read subprocess stdout/stderr line by line into log buffer."""
        if not stream:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                self._log_buffer.append(f"[{label}] {text}")
                if label == "stderr" and text:
                    logger.debug(f"LLM Proxy stderr: {text}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Stream reader ({label}) error: {e}")

    # ── Notifications ───────────────────────────────────────────────────

    async def _notify(self, text: str):
        """Send Telegram notification to owner (if configured)."""
        if not self._tg_notify:
            return
        try:
            await self._tg_notify(text)
        except Exception as e:
            logger.debug(f"Proxy notification failed: {e}")
