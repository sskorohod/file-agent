/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/web/templates/**/*.html"],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg:      { DEFAULT: '#09090b', 2: '#111113' },
        surface: { DEFAULT: '#151518', 2: '#1c1c21', 3: '#232329', hover: '#1a1a1f' },
        border:  { DEFAULT: '#27272a', 2: '#3f3f46' },
        txt:     { DEFAULT: '#fafafa', 2: '#a1a1aa', 3: '#71717a' },
        accent:  { DEFAULT: '#7c3aed', 2: '#a78bfa', 3: '#c4b5fd', hover: '#6d28d9', light: '#ede9fe', bg: 'rgba(124,58,237,0.08)' },
        ok:      { DEFAULT: '#22c55e', bg: 'rgba(34,197,94,0.1)', text: '#4ade80' },
        err:     { DEFAULT: '#ef4444', bg: 'rgba(239,68,68,0.1)', text: '#f87171' },
        warn:    { DEFAULT: '#f59e0b', bg: 'rgba(245,158,11,0.1)', text: '#fbbf24' },
        info:    { DEFAULT: '#3b82f6', bg: 'rgba(59,130,246,0.1)', text: '#60a5fa' },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
      animation: {
        'fade-in': 'fadeIn .2s ease-out',
        'slide-in': 'slideIn .25s ease-out',
        'slide-up': 'slideUp .3s cubic-bezier(.16,1,.3,1)',
        'pulse-soft': 'pulseSoft 2s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
        slideIn: { '0%': { transform: 'translateX(-100%)', opacity: '0' }, '100%': { transform: 'translateX(0)', opacity: '1' } },
        slideUp: { '0%': { transform: 'translateY(100%)' }, '100%': { transform: 'translateY(0)' } },
        pulseSoft: { '0%,100%': { opacity: '1' }, '50%': { opacity: '.6' } },
      },
    }
  },
  plugins: [],
}
