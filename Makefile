PYTHON ?= python3

.PHONY: test wheel wheel-check

test:
	$(PYTHON) -m unittest discover -s tests -p "test_sky130_verify_*.py" -v
	$(PYTHON) -m unittest tests.test_verification_manifest -v

# Build from a temporary copy to exclude AppleDouble resource forks.
wheel:
	rm -rf /tmp/sky130-verify-build-src
	rsync -a --exclude '._*' --exclude '__pycache__' --exclude '*.egg-info' \
		--exclude 'build' --exclude '.git' \
		./ /tmp/sky130-verify-build-src/
	$(PYTHON) -m pip install --upgrade pip setuptools wheel
	cd /tmp/sky130-verify-build-src && $(PYTHON) -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
	mkdir -p dist
	cp /tmp/sky130-verify-build-src/dist/*.whl dist/
	rm -rf /tmp/sky130-verify-build-src

# Installs the wheel into a temporary prefix and calls the real entry point.
wheel-check: wheel
	rm -rf /tmp/sky130-verify-wheel-install
	mkdir -p /tmp/sky130-verify-wheel-install
	$(PYTHON) -m pip install --no-deps --target /tmp/sky130-verify-wheel-install dist/*.whl
	PYTHONPATH=/tmp/sky130-verify-wheel-install $(PYTHON) -m sky130_verify.cli --help >/dev/null
	PYTHONPATH=/tmp/sky130-verify-wheel-install $(PYTHON) -m sky130_verify.cli manifest validate --help >/dev/null
	rm -rf /tmp/sky130-verify-wheel-install
