"""Tests run only against the local mock. Never let them pick up a developer's
real credentials: no .env, no FRONTEGG_* variables, no real settings folder.
Subprocesses started by the tests inherit this environment."""

import atexit
import os
import shutil
import tempfile

for _key in [k for k in os.environ if k.startswith("FRONTEGG_")]:
    del os.environ[_key]
_ISOLATED = tempfile.mkdtemp(prefix="fde-tests-")
atexit.register(shutil.rmtree, _ISOLATED, True)
os.environ["FDE_DOTENV"] = os.path.join(_ISOLATED, "no-such.env")
os.environ["FDE_HOME"] = os.path.join(_ISOLATED, "home")
