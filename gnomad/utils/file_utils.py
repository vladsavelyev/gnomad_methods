# noqa: D100

import asyncio
import base64
import gzip
import logging
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Dict, Optional, Tuple, Union

import hail as hl
from hailtop.aiotools import LocalAsyncFS, AsyncFS
from hailtop.aiotools.router_fs import RouterAsyncFS
from hailtop.aiogoogle import GoogleStorageAsyncFS
from hailtop.utils import bounded_gather, tqdm

logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


async def parallel_file_exists_async(
    fpaths: List[str], parallelism: int = 750
) -> Dict[str, bool]:
    """
    Check whether a large number of files exist.

    Created for use with hail Batch jobs.
    Normal `file_exists` function is very slow when checking a large number of files.

    :param fpaths: List of file paths to check. Files can be in local or Google cloud storage.
    :param parallelism: Integer that sets parallelism of file existence checking task. Default is 750.
    :return: Dictionary of file paths (str) and whether the file exists (boolean).
    """

    async def async_file_exists(fs: AsyncFS, fpath: str) -> bool:
        """
        Determine file existence.

        :param fs: AsyncFS object.
        :param fpath: Path to file to check.
        :return: Whether file exists.
        """
        fext = os.path.splitext(fpath)[1]
        if fext in [".ht", ".mt"]:
            fpath += "/_SUCCESS"
        try:
            await fs.statfile(fpath)
        except FileNotFoundError:
            return False
        else:
            return True

    with tqdm(
        total=len(fpaths), desc="check files for existence", disable=False
    ) as pbar:
        with ThreadPoolExecutor() as thread_pool:
            async with RouterAsyncFS(
                "file", filesystems=[LocalAsyncFS(thread_pool), GoogleStorageAsyncFS()]
            ) as fs:

                def check_existence_and_update_pbar_thunk(fpath: str) -> Callable:
                    """
                    Create function to check if file exists and update progress bar in stdout.

                    Function delays coroutine creation to avoid creating too many live coroutines.

                    :param fpath: Path to file to check.
                    :return: Function that checks for file existence and updates progress bar.
                    """

                    async def unapplied_function():
                        x = await async_file_exists(fs, fpath)
                        pbar.update(1)
                        return x

                    return unapplied_function

                file_existence_checks = [
                    check_existence_and_update_pbar_thunk(fpath) for fpath in fpaths
                ]
                file_existence = await bounded_gather(
                    *file_existence_checks, parallelism=parallelism
                )
    return dict(zip(fpaths, file_existence))


def parallel_file_exists(fpaths: List[str], parallelism: int = 750) -> Dict[str, bool]:
    """
    Call `parallel_file_exists_async` to check whether large number of files exist.

    :param fpaths: List of file paths to check. Files can be in local or Google cloud storage.
    :param parallelism: Integer that sets parallelism of file existence checking task. Default is 750.
    :return: Dictionary of file paths (str) and whether the file exists (boolean).
    """
    return asyncio.get_event_loop().run_until_complete(
        parallel_file_exists_async(fpaths, parallelism)
    )


def file_exists(fname: str) -> bool:
    """
    Check whether a file exists.

    Supports either local or Google cloud (gs://) paths.
    If the file is a Hail file (.ht, .mt extensions), it checks that _SUCCESS is present.

    :param fname: File name
    :return: Whether the file exists
    """
    fext = os.path.splitext(fname)[1]
    if fext in [".ht", ".mt"]:
        fname += "/_SUCCESS"
    if fname.startswith("gs://"):
        return hl.hadoop_exists(fname)
    else:
        return os.path.isfile(fname)


def write_temp_gcs(
    t: Union[hl.MatrixTable, hl.Table],
    gcs_path: str,
    overwrite: bool = False,
    temp_path: Optional[str] = None,
) -> None:  # noqa: D103
    if not temp_path:
        temp_path = f"/tmp_{uuid.uuid4()}.h"
    t.write(temp_path, overwrite=True)
    t = (
        hl.read_matrix_table(temp_path)
        if isinstance(t, hl.MatrixTable)
        else hl.read_table(temp_path)
    )
    t.write(gcs_path, overwrite=overwrite)


def select_primitives_from_ht(ht: hl.Table) -> hl.Table:
    """
    Select only primitive types (string, int, float, bool) from a Table.

    Particularly useful for exporting a Table.

    :param ht: Input Table
    :return: Table with only primitive types selected
    """
    return ht.select(
        **{
            x: v
            for x, v in ht.row_value.items()
            if v.dtype
            in {hl.tstr, hl.tint32, hl.tfloat32, hl.tint64, hl.tfloat64, hl.tbool}
        }
    )


def get_file_stats(url: str, project_id: Optional[str] = None) -> Tuple[int, str, str]:
    """
    Get size (as both int and str) and md5 for file at specified URL.

    Typically used to get stats on VCFs.

    :param url: Path to file of interest.
    :param project_id: Google project ID. Specify if URL points to a requester-pays bucket.
    :return: Tuple of file size and md5.
    """
    one_gibibyte = 2**30
    one_mebibyte = 2**20

    if project_id:
        output = subprocess.check_output(
            ["gsutil", "-u", project_id, "stat", url]
        ).decode("utf8")
    else:
        output = subprocess.check_output(["gsutil", "stat", url]).decode("utf8")
    lines = output.split("\n")

    info = {}
    for line in lines:
        if not line:
            continue

        label, value = [s.strip() for s in line.split(":", 1)]
        if label == "Content-Length":
            size = int(value)
            if size >= one_gibibyte:
                info["size"] = f"{round(size / one_gibibyte, 2)} GiB"
            else:
                info["size"] = f"{round(size / one_mebibyte, 2)} MiB"

        if label == "Hash (md5)":
            info["md5"] = base64.b64decode(value).hex()

    return (size, info["size"], info["md5"])


def read_list_data(input_file_path: str) -> List[str]:
    """
    Read a file input into a python list (each line will be an element).

    Supports Google storage paths and .gz compression.

    :param input_file_path: File path
    :return: List of lines
    """
    if input_file_path.startswith("gs://"):
        hl.hadoop_copy(input_file_path, "file:///" + input_file_path.split("/")[-1])
        f = (
            gzip.open("/" + os.path.basename(input_file_path), encoding="utf-8")
            if input_file_path.endswith("gz")
            else open("/" + os.path.basename(input_file_path), encoding="utf-8")
        )
    else:
        f = (
            gzip.open(input_file_path, encoding="utf-8")
            if input_file_path.endswith("gz")
            else open(input_file_path, encoding="utf-8")
        )
    output = []
    for line in f:
        output.append(line.strip())
    f.close()
    return output
