.PHONY: install test lint synth deploy-dev deploy-staging deploy-prod frontend-build clean

PROJECT_NAME := rag-research-agent

install:
	pip install -r requirements-dev.txt
	cd frontend && npm install

test:
	pytest tests/ -v --cov=src --cov-report=term-missing

lint:
	black --check src/ infrastructure/ tests/
	isort --check-only src/ infrastructure/ tests/
	bandit -r src/ -ll

format:
	black src/ infrastructure/ tests/
	isort src/ infrastructure/ tests/

synth:
	cdk synth

deploy-dev:
	cdk deploy RagResearchAgentDev/* --require-approval never

deploy-pipeline:
	cdk deploy RagResearchAgentPipeline --require-approval broadening

frontend-build:
	cd frontend && npm run build

clean:
	rm -rf cdk.out .pytest_cache __pycache__ .mypy_cache htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
