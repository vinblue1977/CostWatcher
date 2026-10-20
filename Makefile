PYTHON ?= .venv/bin/python
SITE_DIR ?= site
# The CDK CLI pinned in package-lock.json (`make install` runs npm ci).
CDK ?= npx --no-install cdk

.PHONY: install check lint test audit lock synth diff clean site

install:            ## Create the virtualenv and install everything (uv if installed, else pip)
	if command -v uv >/dev/null; then \
		uv venv --seed --allow-existing --python python3.14 .venv && uv pip install --python $(PYTHON) --require-hashes -r requirements-dev.txt && uv pip check --python $(PYTHON); \
	else \
		python3.14 -m venv .venv && $(PYTHON) -m pip install --upgrade pip && $(PYTHON) -m pip install --require-hashes -r requirements-dev.txt && $(PYTHON) -m pip check; \
	fi
	npm ci --ignore-scripts --no-audit --no-fund  # the CDK CLI and pyright
	npm ci --prefix site --ignore-scripts --no-audit --no-fund  # the dashboard's toolchain

check:              ## Everything CI checks, locally (synth uses a stand-in site, like CI)
	$(MAKE) lint test audit site
	$(MAKE) synth SITE_DIR=tests/fixtures/site

site:               ## Dashboard: typecheck, unit tests, production build into site/dist
	npm run --prefix site typecheck
	npm run --prefix site test
	npm run --prefix site build

lint:               ## Python: ruff (with Bandit security rules) and pyright types
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	npx --no-install pyright

test:
	$(PYTHON) -m pytest -n auto

audit:              ## Check dependencies for known vulnerabilities
	$(PYTHON) -m pip_audit --require-hashes --disable-pip -r requirements-dev.txt

lock:               ## Regenerate the hashed lockfiles (CDK app and dev tools) from the .in files
	for d in requirements requirements-dev; do $(PYTHON) -m piptools compile --quiet --generate-hashes --allow-unsafe \
		--strip-extras --no-emit-index-url --output-file=$$d.txt $$d.in; done
	sed -i.bak 's/ --no-index//' requirements.txt requirements-dev.txt && rm requirements.txt.bak requirements-dev.txt.bak

synth:              ## Synthesize CloudFormation and run cdk-nag security checks
	. .venv/bin/activate && $(CDK) synth --quiet -c siteDir=$(SITE_DIR)

diff:
	. .venv/bin/activate && $(CDK) diff -c siteDir=$(SITE_DIR)

clean:
	rm -rf build cdk.out .pytest_cache .ruff_cache .coverage
