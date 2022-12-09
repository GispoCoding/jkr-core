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

    # use local data dir until we get anonymized test fixtures to use
    datadir = testdir.parent / "data" / testfilename
    if os.path.isdir(datadir):
        copytree(datadir, tmpdir, dirs_exist_ok=True)
    print(datadir)
    print(tmpdir)
    print(os.listdir(tmpdir))

    return tmpdir
