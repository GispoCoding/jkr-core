import os
from pathlib import Path
from shutil import copytree

from pytest import fixture


@fixture
def datadir(tmpdir, request):
    """
    Fixture responsible for searching a folder with the same name of test
    module and, if available, moving all contents to a temporary directory so
    tests can use them freely.
    """
    testfilepath = Path(request.module.__file__)
    testdir = testfilepath.parent
    testfilename = testfilepath.stem

    datadir = testdir / "data" / testfilename
    if os.path.isdir(datadir):
        copytree(datadir, tmpdir, dirs_exist_ok=True)

    return tmpdir
