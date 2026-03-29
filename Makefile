.PHONY: install dev test lint clean css css-watch

install:
	pip install -r requirements.txt

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest tests/ -v --tb=short

lint:
	ruff check app/ tests/
	ruff format --check app/ tests/

format:
	ruff check --fix app/ tests/
	ruff format app/ tests/

css:
	npx tailwindcss -i app/web/static/src/input.css -o app/web/static/css/styles.css --minify

css-watch:
	npx tailwindcss -i app/web/static/src/input.css -o app/web/static/css/styles.css --watch

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .ruff_cache
