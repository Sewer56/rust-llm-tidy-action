# Tests for `scripts/`

Plain `unittest` suites; stdlib only, no venv or pytest needed.

Run all commands from `scripts/` (the parent of this directory):

```sh
cd ../            # if you are in tests/
python3 -m unittest discover -s tests -v
```

## Run one module

```sh
python3 -m unittest tests.test_gh_api -v
```

## Run one test case

```sh
python3 -m unittest tests.test_gh_api.GhApiTests.test_sends_method_path_and_json_body -v
```
