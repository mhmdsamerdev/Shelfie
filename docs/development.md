# Development Guide

## Where the version is defined

When releasing a new version, update all 3 places:

1. `pyproject.toml` -> `[project].version`
2. `src/shelfie/__init__.py` -> `__version__`
3. `Dockerfile` -> `LABEL org.opencontainers.image.version="..."`

Package/image names:

- PyPI: `shelfie-py`
- Docker Hub: `mhmdsamerdev/shelfie`

## Release to PyPI

1. Bump version in the 3 files above.
2. clear dist folder `del /s /q dist\*`
3. Build package:

```bash
python -m build
```

4. Check artifacts:

```bash
python -m twine check dist/*
```

5. Upload to PyPI:

```bash
python -m twine upload dist/*
```

6. Verify: https://pypi.org/project/shelfie-py/

## Release to Docker Hub

1. Build and tag (replace `0.2.1` with your new version):

```bash
docker build -t mhmdsamerdev/shelfie:0.2.1 -t mhmdsamerdev/shelfie:latest .
```

2. Push both tags:

```bash
docker push mhmdsamerdev/shelfie:0.2.1
docker push mhmdsamerdev/shelfie:latest
```

3. Verify: https://hub.docker.com/r/mhmdsamerdev/shelfie

## Running Locally

To run the application locally for development:

1. `git clone https://github.com/mhmdsamerdev/shelfie.git` 
2. `uv venv --python 3.12`
3. `source .venv/bin/activate`
4. `uv pip sync -r requirements.txt`
5. `PYTHONPATH=src python -m shelfie.main --reload`