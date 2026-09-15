PYTHON ?= python3

.PHONY: test wheel wheel-check

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py" -v

# Build a wheel with the declared setuptools package data.
wheel:
	mkdir -p dist
	$(PYTHON) -m pip wheel --no-deps --wheel-dir dist .

# Installs the wheel into a temporary prefix and calls the real entry point.
wheel-check: wheel
	install_dir=$$(mktemp -d /tmp/sky130-verify-wheel-install.XXXXXX); \
	trap 'rm -rf "$$install_dir"' EXIT; \
	$(PYTHON) -m pip install --no-deps --target "$$install_dir" dist/*.whl; \
	PYTHONPATH="$$install_dir" $(PYTHON) -m sky130_verify.cli --help >/dev/null; \
	PYTHONPATH="$$install_dir" $(PYTHON) -m sky130_verify.cli manifest validate --help >/dev/null
