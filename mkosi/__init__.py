# SPDX-License-Identifier: LGPL-2.1+

from __future__ import annotations

import argparse
import ast
import collections
import configparser
import contextlib
import crypt
import ctypes
import ctypes.util
import dataclasses
import datetime
import errno
import fcntl
import functools
import getpass
import glob
import hashlib
import http.server
import importlib.resources
import json
import math
import os
import platform
import re
import shlex
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from subprocess import DEVNULL, PIPE
from textwrap import dedent
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Callable,
    ContextManager,
    Deque,
    Dict,
    Generator,
    Iterable,
    List,
    NamedTuple,
    NoReturn,
    Optional,
    Sequence,
    Set,
    TextIO,
    Tuple,
    TypeVar,
    Union,
    cast,
)

from .backend import (
    ARG_DEBUG,
    CommandLineArguments,
    Distribution,
    ManifestFormat,
    MkosiException,
    MkosiPrinter,
    OutputFormat,
    SourceFileTransfer,
    die,
    install_grub,
    nspawn_params_for_blockdev_access,
    partition,
    patch_file,
    path_relative_to_cwd,
    run,
    run_with_backoff,
    run_workspace_command,
    should_compress_fs,
    should_compress_output,
    spawn,
    tmp_dir,
    var_tmp,
    warn,
    workspace,
    write_grub_config,
)
from .manifest import Manifest

complete_step = MkosiPrinter.complete_step

__version__ = "10"


# These types are only generic during type checking and not at runtime, leading
# to a TypeError during compilation.
# Let's be as strict as we can with the description for the usage we have.
if TYPE_CHECKING:
    CompletedProcess = subprocess.CompletedProcess[Any]
    TempDir = tempfile.TemporaryDirectory[str]
else:
    CompletedProcess = subprocess.CompletedProcess
    TempDir = tempfile.TemporaryDirectory

SomeIO = Union[BinaryIO, TextIO]
PathString = Union[Path, str]

MKOSI_COMMANDS_CMDLINE = ("build", "shell", "boot", "qemu", "ssh")
MKOSI_COMMANDS_NEED_BUILD = ("shell", "boot", "qemu", "serve")
MKOSI_COMMANDS_SUDO = ("build", "clean", "shell", "boot", "qemu", "serve")
MKOSI_COMMANDS = ("build", "clean", "help", "summary", "genkey", "bump", "serve") + MKOSI_COMMANDS_CMDLINE

DRACUT_SYSTEMD_EXTRAS = [
    "/usr/bin/systemd-repart",
    "/usr/lib/systemd/system-generators/systemd-veritysetup-generator",
    "/usr/lib/systemd/system/initrd-root-fs.target.wants/systemd-repart.service",
    "/usr/lib/systemd/system/initrd-usr-fs.target",
    "/usr/lib/systemd/system/systemd-repart.service",
    "/usr/lib/systemd/system/systemd-volatile-root.service",
    "/usr/lib/systemd/systemd-veritysetup",
    "/usr/lib/systemd/systemd-volatile-root",
]


def write_resource(
        where: Path, resource: str, key: str, *, executable: bool = False, mode: Optional[int] = None
) -> None:
    text = importlib.resources.read_text(resource, key)
    where.write_text(text)
    if mode is not None:
        where.chmod(mode)
    elif executable:
        make_executable(where)


T = TypeVar("T")
V = TypeVar("V")


def print_between_lines(s: str) -> None:
    size = os.get_terminal_size()
    print('-' * size.columns)
    print(s.rstrip('\n'))
    print('-' * size.columns)


def dictify(f: Callable[..., Generator[Tuple[T, V], None, None]]) -> Callable[..., Dict[T, V]]:
    def wrapper(*args: Any, **kwargs: Any) -> Dict[T, V]:
        return dict(f(*args, **kwargs))

    return functools.update_wrapper(wrapper, f)


@dictify
def read_os_release() -> Generator[Tuple[str, str], None, None]:
    try:
        filename = "/etc/os-release"
        f = open(filename)
    except FileNotFoundError:
        filename = "/usr/lib/os-release"
        f = open(filename)

    for line_number, line in enumerate(f, start=1):
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Z][A-Z_0-9]+)=(.*)", line)
        if m:
            name, val = m.groups()
            if val and val[0] in "\"'":
                val = ast.literal_eval(val)
            yield name, val
        else:
            print(f"{filename}:{line_number}: bad line {line!r}", file=sys.stderr)


def print_running_cmd(cmdline: Iterable[str]) -> None:
    MkosiPrinter.print_step("Running command:")
    MkosiPrinter.print_step(" ".join(shlex.quote(x) for x in cmdline) + "\n")


GPT_ROOT_X86           = uuid.UUID("44479540f29741b29af7d131d5f0458a")  # NOQA: E221
GPT_ROOT_X86_64        = uuid.UUID("4f68bce3e8cd4db196e7fbcaf984b709")  # NOQA: E221
GPT_ROOT_ARM           = uuid.UUID("69dad7102ce44e3cb16c21a1d49abed3")  # NOQA: E221
GPT_ROOT_ARM_64        = uuid.UUID("b921b0451df041c3af444c6f280d3fae")  # NOQA: E221
GPT_USR_X86            = uuid.UUID("75250d768cc6458ebd66bd47cc81a812")  # NOQA: E221
GPT_USR_X86_64         = uuid.UUID("8484680c952148c69c11b0720656f69e")  # NOQA: E221
GPT_USR_ARM            = uuid.UUID("7d0359a302b34f0a865c654403e70625")  # NOQA: E221
GPT_USR_ARM_64         = uuid.UUID("b0e01050ee5f4390949a9101b17104e9")  # NOQA: E221
GPT_ESP                = uuid.UUID("c12a7328f81f11d2ba4b00a0c93ec93b")  # NOQA: E221
GPT_BIOS               = uuid.UUID("2168614864496e6f744e656564454649")  # NOQA: E221
GPT_SWAP               = uuid.UUID("0657fd6da4ab43c484e50933c84b4f4f")  # NOQA: E221
GPT_HOME               = uuid.UUID("933ac7e12eb44f13b8440e14e2aef915")  # NOQA: E221
GPT_SRV                = uuid.UUID("3b8f842520e04f3b907f1a25a76f98e8")  # NOQA: E221
GPT_XBOOTLDR           = uuid.UUID("bc13c2ff59e64262a352b275fd6f7172")  # NOQA: E221
GPT_ROOT_X86_VERITY    = uuid.UUID("d13c5d3bb5d1422ab29f9454fdc89d76")  # NOQA: E221
GPT_ROOT_X86_64_VERITY = uuid.UUID("2c7357edebd246d9aec123d437ec2bf5")  # NOQA: E221
GPT_ROOT_ARM_VERITY    = uuid.UUID("7386cdf2203c47a9a498f2ecce45a2d6")  # NOQA: E221
GPT_ROOT_ARM_64_VERITY = uuid.UUID("df3300ced69f4c92978c9bfb0f38d820")  # NOQA: E221
GPT_USR_X86_VERITY     = uuid.UUID("8f461b0d14ee4e819aa9049b6fb97abd")  # NOQA: E221
GPT_USR_X86_64_VERITY  = uuid.UUID("77ff5f63e7b64633acf41565b864c0e6")  # NOQA: E221
GPT_USR_ARM_VERITY     = uuid.UUID("c215d7517bcd4649be906627490a4c05")  # NOQA: E221
GPT_USR_ARM_64_VERITY  = uuid.UUID("6e11a4e7fbca4dedb9e9e1a512bb664e")  # NOQA: E221
GPT_TMP                = uuid.UUID("7ec6f5573bc54acab29316ef5df639d1")  # NOQA: E221
GPT_VAR                = uuid.UUID("4d21b016b53445c2a9fb5c16e091fd2d")  # NOQA: E221


# This is a non-formatted partition used to store the second stage
# part of the bootloader because it doesn't necessarily fits the MBR
# available space. 1MiB is more than enough for our usages and there's
# little reason for customization since it only stores the bootloader and
# not user-owned configuration files or kernels. See
# https://en.wikipedia.org/wiki/BIOS_boot_partition
# and https://www.gnu.org/software/grub/manual/grub/html_node/BIOS-installation.html
BIOS_PARTITION_SIZE = 1024 * 1024

CLONE_NEWNS = 0x00020000

FEDORA_KEYS_MAP = {
    "7":  "CAB44B996F27744E86127CDFB44269D04F2A6FD2",
    "8":  "4FFF1F04010DEDCAE203591D62AEC3DC6DF2196F",
    "9":  "4FFF1F04010DEDCAE203591D62AEC3DC6DF2196F",
    "10": "61A8ABE091FF9FBBF4B07709BF226FCC4EBFC273",
    "11": "AEE40C04E34560A71F043D7C1DC5C758D22E77F2",
    "12": "6BF178D28A789C74AC0DC63B9D1CC34857BBCCBA",
    "13": "8E5F73FF2A1817654D358FCA7EDC6AD6E8E40FDE",
    "14": "235C2936B4B70E61B373A020421CADDB97A1071F",
    "15": "25DBB54BDED70987F4C10042B4EBF579069C8460",
    "16": "05A912AC70457C3DBC82D352067F00B6A82BA4B7",
    "17": "CAC43FB774A4A673D81C5DE750E94C991ACA3465",
    "18": "7EFB8811DD11E380B679FCEDFF01125CDE7F38BD",
    "19": "CA81B2C85E4F4D4A1A3F723407477E65FB4B18E6",
    "20": "C7C9A9C89153F20183CE7CBA2EB161FA246110C1",
    "21": "6596B8FBABDA5227A9C5B59E89AD4E8795A43F54",
    "22": "C527EA07A9349B589C35E1BF11ADC0948E1431D5",
    "23": "EF45510680FB02326B045AFB32474CF834EC9CBA",
    "24": "5048BDBBA5E776E547B09CCC73BDE98381B46521",
    "25": "C437DCCD558A66A37D6F43724089D8F2FDB19C98",
    "26": "E641850B77DF435378D1D7E2812A6B4B64DAB85D",
    "27": "860E19B0AFA800A1751881A6F55E7430F5282EE4",
    "28": "128CF232A9371991C8A65695E08E7E629DB62FB1",
    "29": "5A03B4DD8254ECA02FDA1637A20AA56B429476B4",
    "30": "F1D8EC98F241AAF20DF69420EF3C111FCFC659B9",
    "31": "7D22D5867F2A4236474BF7B850CB390B3C3359C4",
    "32": "97A1AE57C3A2372CCA3A4ABA6C13026D12C944D0",
    "33": "963A2BEB02009608FE67EA4249FD77499570FF31",
    "34": "8C5BA6990BDB26E19F2A1A801161AE6945719A39",
    "35": "787EA6AE1147EEE56C40B30CDB4639719867C58F",
    "36": "53DED2CB922D8B8D9E63FD18999F7CBF38AB71F4",
}


# Debian calls their architectures differently, so when calling debootstrap we
# will have to map to their names
DEBIAN_ARCHITECTURES = {
    "x86_64": "amd64",
    "x86": "i386",
    "aarch64": "arm64",
    "armhfp": "armhf",
}


class GPTRootTypePair(NamedTuple):
    root: uuid.UUID
    verity: uuid.UUID


def gpt_root_native(arch: Optional[str], usr_only: bool = False) -> GPTRootTypePair:
    """The tag for the native GPT root partition for the given architecture

    Returns a tuple of two tags: for the root partition and for the
    matching verity partition.
    """
    if arch is None:
        arch = platform.machine()

    if usr_only:
        if arch in ("i386", "i486", "i586", "i686"):
            return GPTRootTypePair(GPT_USR_X86, GPT_USR_X86_VERITY)
        elif arch == "x86_64":
            return GPTRootTypePair(GPT_USR_X86_64, GPT_USR_X86_64_VERITY)
        elif arch == "aarch64":
            return GPTRootTypePair(GPT_USR_ARM_64, GPT_USR_ARM_64_VERITY)
        elif arch == "armv7l":
            return GPTRootTypePair(GPT_USR_ARM, GPT_USR_ARM_VERITY)
        else:
            die(f"Unknown architecture {arch}.")
    else:
        if arch in ("i386", "i486", "i586", "i686"):
            return GPTRootTypePair(GPT_ROOT_X86, GPT_ROOT_X86_VERITY)
        elif arch == "x86_64":
            return GPTRootTypePair(GPT_ROOT_X86_64, GPT_ROOT_X86_64_VERITY)
        elif arch == "aarch64":
            return GPTRootTypePair(GPT_ROOT_ARM_64, GPT_ROOT_ARM_64_VERITY)
        elif arch == "armv7l":
            return GPTRootTypePair(GPT_ROOT_ARM, GPT_ROOT_ARM_VERITY)
        else:
            die(f"Unknown architecture {arch}.")


def roothash_suffix(usr_only: bool = False) -> str:
    if usr_only:
        return ".usrhash"

    return ".roothash"


def unshare(flags: int) -> None:
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        die("Could not find libc")
    libc = ctypes.CDLL(libc_name, use_errno=True)

    if libc.unshare(ctypes.c_int(flags)) != 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))


def format_bytes(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024 * 1024:
        return f"{num_bytes/1024**3 :0.1f}G"
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes/1024**2 :0.1f}M"
    if num_bytes >= 1024:
        return f"{num_bytes/1024 :0.1f}K"

    return f"{num_bytes}B"


def roundup(x: int, step: int) -> int:
    return ((x + step - 1) // step) * step


_IOC_NRBITS   =  8  # NOQA: E221,E222
_IOC_TYPEBITS =  8  # NOQA: E221,E222
_IOC_SIZEBITS = 14  # NOQA: E221,E222
_IOC_DIRBITS  =  2  # NOQA: E221,E222

_IOC_NRSHIFT   = 0  # NOQA: E221
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS  # NOQA: E221
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS  # NOQA: E221
_IOC_DIRSHIFT  = _IOC_SIZESHIFT + _IOC_SIZEBITS  # NOQA: E221

_IOC_NONE  = 0  # NOQA: E221
_IOC_WRITE = 1  # NOQA: E221
_IOC_READ  = 2  # NOQA: E221


def _IOC(dir_rw: int, type_drv: int, nr: int, argtype: str) -> int:
    size = {"int": 4, "size_t": 8}[argtype]
    return dir_rw << _IOC_DIRSHIFT | type_drv << _IOC_TYPESHIFT | nr << _IOC_NRSHIFT | size << _IOC_SIZESHIFT


def _IOW(type_drv: int, nr: int, size: str) -> int:
    return _IOC(_IOC_WRITE, type_drv, nr, size)


FICLONE = _IOW(0x94, 9, "int")


@contextlib.contextmanager
def open_close(path: PathString, flags: int, mode: int = 0o664) -> Generator[int, None, None]:
    fd = os.open(path, flags | os.O_CLOEXEC, mode)
    try:
        yield fd
    finally:
        os.close(fd)


def _reflink(oldfd: int, newfd: int) -> None:
    fcntl.ioctl(newfd, FICLONE, oldfd)


def copy_fd(oldfd: int, newfd: int) -> None:
    try:
        _reflink(oldfd, newfd)
    except OSError as e:
        if e.errno not in {errno.EXDEV, errno.EOPNOTSUPP}:
            raise
        # While mypy handles this correctly, Pyright doesn't yet.
        shutil.copyfileobj(open(oldfd, "rb", closefd=False), cast(Any, open(newfd, "wb", closefd=False)))


def copy_file_object(oldobject: BinaryIO, newobject: BinaryIO) -> None:
    try:
        _reflink(oldobject.fileno(), newobject.fileno())
    except OSError as e:
        if e.errno not in {errno.EXDEV, errno.EOPNOTSUPP}:
            raise
        shutil.copyfileobj(oldobject, newobject)


def copy_file(oldpath: PathString, newpath: PathString) -> None:
    oldpath = Path(oldpath)
    newpath = Path(newpath)

    if oldpath.is_symlink():
        src = os.readlink(oldpath)  # TODO: use oldpath.readlink() with python3.9+
        newpath.symlink_to(src)
        return

    with open_close(oldpath, os.O_RDONLY) as oldfd:
        st = os.stat(oldfd)

        try:
            with open_close(newpath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, st.st_mode) as newfd:
                copy_fd(oldfd, newfd)
        except FileExistsError:
            newpath.unlink()
            with open_close(newpath, os.O_WRONLY | os.O_CREAT, st.st_mode) as newfd:
                copy_fd(oldfd, newfd)
    shutil.copystat(oldpath, newpath, follow_symlinks=False)


def symlink_f(target: str, path: Path) -> None:
    try:
        path.symlink_to(target)
    except FileExistsError:
        os.unlink(path)
        path.symlink_to(target)


def copy_path(oldpath: PathString, newpath: Path) -> None:
    try:
        newpath.mkdir(exist_ok=True)
    except FileExistsError:
        # something that is not a directory already exists
        newpath.unlink()
        newpath.mkdir()

    for entry in os.scandir(oldpath):
        newentry = newpath / entry.name
        if entry.is_dir(follow_symlinks=False):
            copy_path(entry.path, newentry)
        elif entry.is_symlink():
            target = os.readlink(entry.path)
            symlink_f(target, newentry)
            shutil.copystat(entry.path, newentry, follow_symlinks=False)
        else:
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                copy_file(entry.path, newentry)
            else:
                print("Ignoring", entry.path)
                continue
    shutil.copystat(oldpath, newpath, follow_symlinks=True)


@complete_step("Detaching namespace")
def init_namespace(args: CommandLineArguments) -> None:
    unshare(CLONE_NEWNS)
    run(["mount", "--make-rslave", "/"])


def setup_workspace(args: CommandLineArguments) -> TempDir:
    with complete_step("Setting up temporary workspace.", "Temporary workspace set up in {.name}") as output:
        if args.workspace_dir is not None:
            d = tempfile.TemporaryDirectory(dir=args.workspace_dir, prefix="")
        elif args.output_format in (OutputFormat.directory, OutputFormat.subvolume):
            d = tempfile.TemporaryDirectory(dir=args.output.parent, prefix=".mkosi-")
        else:
            d = tempfile.TemporaryDirectory(dir=tmp_dir(), prefix="mkosi-")
        output.append(d)

    return d


def btrfs_subvol_create(path: Path, mode: int = 0o755) -> None:
    m = os.umask(~mode & 0o7777)
    run(["btrfs", "subvol", "create", path])
    os.umask(m)


def btrfs_subvol_delete(path: Path) -> None:
    # Extract the path of the subvolume relative to the filesystem
    c = run(["btrfs", "subvol", "show", path], stdout=PIPE, stderr=DEVNULL, universal_newlines=True)
    subvol_path = c.stdout.splitlines()[0]
    # Make the subvolume RW again if it was set RO by btrfs_subvol_delete
    run(["btrfs", "property", "set", path, "ro", "false"])
    # Recursively delete the direct children of the subvolume
    c = run(["btrfs", "subvol", "list", "-o", path], stdout=PIPE, stderr=DEVNULL, universal_newlines=True)
    for line in c.stdout.splitlines():
        if not line:
            continue
        child_subvol_path = line.split(" ", 8)[-1]
        child_path = path / cast(str, os.path.relpath(child_subvol_path, subvol_path))
        btrfs_subvol_delete(child_path)
    # Delete the subvolume now that all its descendants have been deleted
    run(["btrfs", "subvol", "delete", path], stdout=DEVNULL, stderr=DEVNULL)


def btrfs_subvol_make_ro(path: Path, b: bool = True) -> None:
    run(["btrfs", "property", "set", path, "ro", "true" if b else "false"])


@contextlib.contextmanager
def btrfs_forget_stale_devices(args: CommandLineArguments) -> Generator[None, None, None]:
    # When using cached images (-i), mounting btrfs images would sometimes fail
    # with EEXIST. This is likely because a stale device is leftover somewhere
    # from the previous run. To fix this, we make sure to always clean up stale
    # btrfs devices after unmounting the image.
    try:
        yield
    finally:
        if args.output_format.is_btrfs() and shutil.which("btrfs"):
            run(["btrfs", "device", "scan", "-u"])


def is_generated_root(args: Union[argparse.Namespace, CommandLineArguments]) -> bool:
    """Returns whether this configuration means we need to generate a file system from a prepared tree

    This is needed for anything squashfs and when root minimization is required."""
    return args.minimize or args.output_format.is_squashfs() or args.usr_only


def image_size(args: CommandLineArguments) -> int:
    gpt = PartitionTable.empty(args.gpt_first_lba)
    size = gpt.first_usable_offset() + gpt.footer_size()

    if args.root_size is not None:
        size += args.root_size
    if args.home_size is not None:
        size += args.home_size
    if args.srv_size is not None:
        size += args.srv_size
    if args.var_size is not None:
        size += args.var_size
    if args.tmp_size is not None:
        size += args.tmp_size
    if args.bootable:
        if "uefi" in args.boot_protocols:
            assert args.esp_size
            size += args.esp_size
        if "bios" in args.boot_protocols:
            size += BIOS_PARTITION_SIZE
    if args.xbootldr_size is not None:
        size += args.xbootldr_size
    if args.swap_size is not None:
        size += args.swap_size
    if args.verity_size is not None:
        size += args.verity_size

    return size


def disable_cow(path: PathString) -> None:
    """Disable copy-on-write if applicable on filesystem"""

    run(["chattr", "+C", path], stdout=DEVNULL, stderr=DEVNULL, check=False)


def root_partition_name(
    args: Optional[CommandLineArguments],
    verity: Optional[bool] = False,
    image_id: Optional[str] = None,
    image_version: Optional[str] = None,
    usr_only: Optional[bool] = False,
) -> str:

    # Support invocation with "args" or with separate parameters (which is useful when invoking it before we allocated a CommandLineArguments object)
    if args is not None:
        image_id = args.image_id
        image_version = args.image_version
        usr_only = args.usr_only

    # We implement two naming regimes for the partitions. If image_id
    # is specified we assume that there's a naming and maybe
    # versioning regime for the image in place, and thus use that to
    # generate the image. If not we pick descriptive names instead.

    # If an image id is specified, let's generate the root, /usr/ or
    # verity partition name from it, in a uniform way for all three
    # types. The image ID is after all a great way to identify what is
    # *in* the image, while the partition type UUID indicates what
    # *kind* of data it is. If we also have a version we include it
    # too. The latter is particularly useful for systemd's image
    # dissection logic, which will always pick the newest root or
    # /usr/ partition if multiple exist.
    if image_id is not None:
        if image_version is not None:
            return f"{image_id}_{image_version}"
        else:
            return image_id

    # If no image id is specified we just return a descriptive string
    # for the partition.
    prefix = "System Resources" if usr_only else "Root"
    if verity:
        return prefix + " Verity"
    return prefix + " Partition"


def determine_partition_table(args: CommandLineArguments) -> Tuple[str, bool]:
    pn = 1
    table = "label: gpt\n"
    if args.gpt_first_lba is not None:
        table += f"first-lba: {args.gpt_first_lba:d}\n"
    run_sfdisk = False
    args.esp_partno = None
    args.bios_partno = None

    if args.bootable:
        if "uefi" in args.boot_protocols:
            assert args.esp_size is not None
            table += f'size={args.esp_size // 512}, type={GPT_ESP}, name="ESP System Partition"\n'
            args.esp_partno = pn
            pn += 1

        if "bios" in args.boot_protocols:
            table += f'size={BIOS_PARTITION_SIZE // 512}, type={GPT_BIOS}, name="BIOS Boot Partition"\n'
            args.bios_partno = pn
            pn += 1

        run_sfdisk = True

    if args.xbootldr_size is not None:
        table += f'size={args.xbootldr_size // 512}, type={GPT_XBOOTLDR}, name="Boot Loader Partition"\n'
        args.xbootldr_partno = pn
        pn += 1
    else:
        args.xbootldr_partno = None

    if args.swap_size is not None:
        table += f'size={args.swap_size // 512}, type={GPT_SWAP}, name="Swap Partition"\n'
        args.swap_partno = pn
        pn += 1
        run_sfdisk = True
    else:
        args.swap_partno = None

    args.home_partno = None
    args.srv_partno = None
    args.var_partno = None
    args.tmp_partno = None

    if args.output_format != OutputFormat.gpt_btrfs:
        if args.home_size is not None:
            table += f'size={args.home_size // 512}, type={GPT_HOME}, name="Home Partition"\n'
            args.home_partno = pn
            pn += 1
            run_sfdisk = True

        if args.srv_size is not None:
            table += f'size={args.srv_size // 512}, type={GPT_SRV}, name="Server Data Partition"\n'
            args.srv_partno = pn
            pn += 1
            run_sfdisk = True

        if args.var_size is not None:
            table += f'size={args.var_size // 512}, type={GPT_VAR}, name="Variable Data Partition"\n'
            args.var_partno = pn
            pn += 1
            run_sfdisk = True

        if args.tmp_size is not None:
            table += f'size={args.tmp_size // 512}, type={GPT_TMP}, name="Temporary Data Partition"\n'
            args.tmp_partno = pn
            pn += 1
            run_sfdisk = True

    if not is_generated_root(args):
        table += 'type={}, attrs={}, name="{}"\n'.format(
            gpt_root_native(args.architecture, args.usr_only).root,
            "GUID:60" if args.read_only and args.output_format != OutputFormat.gpt_btrfs else "",
            root_partition_name(args),
        )
        run_sfdisk = True

    args.root_partno = pn
    pn += 1

    if args.verity:
        args.verity_partno = pn
        pn += 1
    else:
        args.verity_partno = None

    return table, run_sfdisk


def exec_sfdisk(args: CommandLineArguments, f: BinaryIO) -> None:

    table, run_sfdisk = determine_partition_table(args)

    if run_sfdisk:
        run(["sfdisk", "--color=never", f.name], input=table.encode("utf-8"))
        run(["sync"])

    args.ran_sfdisk = run_sfdisk


def create_image(args: CommandLineArguments, for_cache: bool) -> Optional[BinaryIO]:
    if not args.output_format.is_disk():
        return None

    with complete_step(
        "Creating image with partition table…", "Created image with partition table as {.name}"
    ) as output:

        f: BinaryIO = cast(
            BinaryIO,
            tempfile.NamedTemporaryFile(prefix=".mkosi-", delete=not for_cache, dir=args.output.parent),
        )
        output.append(f)
        disable_cow(f.name)
        f.truncate(image_size(args))

        exec_sfdisk(args, f)

    return f


def refresh_partition_table(args: CommandLineArguments, f: BinaryIO) -> None:
    if not args.output_format.is_disk():
        return

    # Let's refresh all UUIDs and labels to match the new build. This
    # is called whenever we reuse a cached image, to ensure that the
    # UUIDs/labels of partitions are generated the same way as for
    # non-cached builds. Note that we refresh the UUIDs/labels simply
    # by invoking sfdisk again. If the build parameters didn't change
    # this should have the effect that offsets and sizes should remain
    # identical, and we thus only update the UUIDs and labels.
    #
    # FIXME: One of those days we should generate the UUIDs as hashes
    # of the used configuration, so that they remain stable as the
    # configuration is identical.

    with complete_step("Refreshing partition table…", "Refreshed partition table."):
        exec_sfdisk(args, f)


def refresh_file_system(args: CommandLineArguments, dev: Optional[Path], cached: bool) -> None:

    if dev is None:
        return
    if not cached:
        return

    # Similar to refresh_partition_table() but refreshes the UUIDs of
    # the file systems themselves. We want that build artifacts from
    # cached builds are as similar as possible to those from uncached
    # builds, and hence we want to randomize UUIDs explicitly like
    # they are for uncached builds. This is particularly relevant for
    # btrfs since it prohibits mounting multiple file systems at the
    # same time that carry the same UUID.
    #
    # FIXME: One of those days we should generate the UUIDs as hashes
    # of the used configuration, so that they remain stable as the
    # configuration is identical.

    with complete_step(f"Refreshing file system {dev}…"):
        if args.output_format == OutputFormat.gpt_btrfs:
            # We use -M instead of -m here, for compatibility with
            # older btrfs, where -M didn't exist yet.
            run(["btrfstune", "-M", str(uuid.uuid4()), dev])
        elif args.output_format == OutputFormat.gpt_ext4:
            # We connect stdin to /dev/null since tune2fs otherwise
            # asks an unnecessary safety question on stdin, and we
            # don't want that, our script doesn't operate on essential
            # file systems anyway, but just our build images.
            run(["tune2fs", "-U", "random", dev], stdin=subprocess.DEVNULL)
        elif args.output_format == OutputFormat.gpt_xfs:
            run(["xfs_admin", "-U", "generate", dev])


def copy_image_temporary(src: Path, dir: Path) -> BinaryIO:
    with src.open("rb") as source:
        f: BinaryIO = cast(BinaryIO, tempfile.NamedTemporaryFile(prefix=".mkosi-", dir=dir))

        # So on one hand we want CoW off, since this stuff will
        # have a lot of random write accesses. On the other we
        # want the copy to be snappy, hence we do want CoW. Let's
        # ask for both, and let the kernel figure things out:
        # let's turn off CoW on the file, but start with a CoW
        # copy. On btrfs that works: the initial copy is made as
        # CoW but later changes do not result in CoW anymore.

        disable_cow(f.name)
        copy_file_object(source, f)

        return f


def copy_file_temporary(src: PathString, dir: Path) -> BinaryIO:
    with open(src, "rb") as source:
        f = cast(BinaryIO, tempfile.NamedTemporaryFile(prefix=".mkosi-", dir=dir))
        copy_file_object(source, f)
        return f


def reuse_cache_image(
    args: CommandLineArguments, do_run_build_script: bool, for_cache: bool
) -> Tuple[Optional[BinaryIO], bool]:
    if not args.incremental:
        return None, False
    if not args.output_format.is_disk_rw():
        return None, False

    fname = args.cache_pre_dev if do_run_build_script else args.cache_pre_inst
    if for_cache:
        if fname and os.path.exists(fname):
            # Cache already generated, skip generation, note that manually removing the exising cache images is
            # necessary if Packages or BuildPackages change
            return None, True
        else:
            return None, False

    if fname is None:
        return None, False

    with complete_step(f"Basing off cached image {fname}", "Copied cached image as {.name}") as output:

        try:
            f = copy_image_temporary(src=fname, dir=args.output.parent)
        except FileNotFoundError:
            return None, False

        output.append(f)
        _, run_sfdisk = determine_partition_table(args)
        args.ran_sfdisk = run_sfdisk

    return f, True


@contextlib.contextmanager
def attach_image_loopback(
    args: CommandLineArguments, raw: Optional[BinaryIO]
) -> Generator[Optional[Path], None, None]:
    if raw is None:
        yield None
        return

    with complete_step("Attaching image file…", "Attached image file as {}") as output:
        c = run(["losetup", "--find", "--show", "--partscan", raw.name],
                stdout=PIPE,
                universal_newlines=True)
        loopdev = Path(c.stdout.strip())
        output.append(loopdev)

    try:
        yield loopdev
    finally:
        with complete_step("Detaching image file"):
            run(["losetup", "--detach", loopdev])


def optional_partition(loopdev: Path, partno: Optional[int]) -> Optional[Path]:
    if partno is None:
        return None

    return partition(loopdev, partno)


def prepare_swap(args: CommandLineArguments, loopdev: Optional[Path], cached: bool) -> None:
    if loopdev is None:
        return
    if cached:
        return
    if args.swap_partno is None:
        return

    with complete_step("Formatting swap partition"):
        run(["mkswap", "-Lswap", partition(loopdev, args.swap_partno)])


def prepare_esp(args: CommandLineArguments, loopdev: Optional[Path], cached: bool) -> None:
    if loopdev is None:
        return
    if cached:
        return
    if args.esp_partno is None:
        return

    with complete_step("Formatting ESP partition"):
        run(["mkfs.fat", "-nEFI", "-F32", partition(loopdev, args.esp_partno)])


def prepare_xbootldr(args: CommandLineArguments, loopdev: Optional[Path], cached: bool) -> None:
    if loopdev is None:
        return
    if cached:
        return
    if args.xbootldr_partno is None:
        return

    with complete_step("Formatting XBOOTLDR partition"):
        run(["mkfs.fat", "-nXBOOTLDR", "-F32", partition(loopdev, args.xbootldr_partno)])


def mkfs_ext4_cmd(label: str, mount: PathString) -> List[str]:
    return ["mkfs.ext4", "-I", "256", "-L", label, "-M", str(mount)]


def mkfs_xfs_cmd(label: str) -> List[str]:
    return ["mkfs.xfs", "-n", "ftype=1", "-L", label]


def mkfs_btrfs_cmd(label: str) -> List[str]:
    return ["mkfs.btrfs", "-L", label, "-d", "single", "-m", "single"]


def mkfs_generic(args: CommandLineArguments, label: str, mount: PathString, dev: Path) -> None:
    cmdline: Sequence[PathString]

    if args.output_format == OutputFormat.gpt_btrfs:
        cmdline = mkfs_btrfs_cmd(label)
    elif args.output_format == OutputFormat.gpt_xfs:
        cmdline = mkfs_xfs_cmd(label)
    else:
        cmdline = mkfs_ext4_cmd(label, mount)

    if args.output_format == OutputFormat.gpt_ext4:
        if args.distribution in (Distribution.centos, Distribution.centos_epel) and is_older_than_centos8(
            args.release
        ):
            # e2fsprogs in centos7 is too old and doesn't support this feature
            cmdline += ["-O", "^metadata_csum"]

        if args.architecture in ("x86_64", "aarch64"):
            # enable 64bit filesystem feature on supported architectures
            cmdline += ["-O", "64bit"]

    run([*cmdline, dev])


def luks_format(dev: Path, passphrase: Dict[str, str]) -> None:
    if passphrase["type"] == "stdin":
        passphrase_content = (passphrase["content"] + "\n").encode("utf-8")
        run(
            [
                "cryptsetup",
                "luksFormat",
                "--force-password",
                "--pbkdf-memory=64",
                "--pbkdf-parallel=1",
                "--pbkdf-force-iterations=1000",
                "--batch-mode",
                dev,
            ],
            input=passphrase_content,
        )
    else:
        assert passphrase["type"] == "file"
        run(
            [
                "cryptsetup",
                "luksFormat",
                "--force-password",
                "--pbkdf-memory=64",
                "--pbkdf-parallel=1",
                "--pbkdf-force-iterations=1000",
                "--batch-mode",
                dev,
                passphrase["content"],
            ]
        )


def luks_format_root(
    args: CommandLineArguments,
    loopdev: Path,
    do_run_build_script: bool,
    cached: bool,
    inserting_generated_root: bool = False,
) -> None:
    if args.encrypt != "all":
        return
    if args.root_partno is None:
        return
    if is_generated_root(args) and not inserting_generated_root:
        return
    if do_run_build_script:
        return
    if cached:
        return
    assert args.passphrase is not None

    with complete_step("Setting up LUKS on root partition…"):
        luks_format(partition(loopdev, args.root_partno), args.passphrase)


def luks_format_home(args: CommandLineArguments, loopdev: Path, do_run_build_script: bool, cached: bool) -> None:
    if args.encrypt is None:
        return
    if args.home_partno is None:
        return
    if do_run_build_script:
        return
    if cached:
        return
    assert args.passphrase is not None

    with complete_step("Setting up LUKS on home partition…"):
        luks_format(partition(loopdev, args.home_partno), args.passphrase)


def luks_format_srv(args: CommandLineArguments, loopdev: Path, do_run_build_script: bool, cached: bool) -> None:
    if args.encrypt is None:
        return
    if args.srv_partno is None:
        return
    if do_run_build_script:
        return
    if cached:
        return
    assert args.passphrase is not None

    with complete_step("Setting up LUKS on server data partition…"):
        luks_format(partition(loopdev, args.srv_partno), args.passphrase)


def luks_format_var(args: CommandLineArguments, loopdev: Path, do_run_build_script: bool, cached: bool) -> None:
    if args.encrypt is None:
        return
    if args.var_partno is None:
        return
    if do_run_build_script:
        return
    if cached:
        return
    assert args.passphrase is not None

    with complete_step("Setting up LUKS on variable data partition…"):
        luks_format(partition(loopdev, args.var_partno), args.passphrase)


def luks_format_tmp(args: CommandLineArguments, loopdev: Path, do_run_build_script: bool, cached: bool) -> None:
    if args.encrypt is None:
        return
    if args.tmp_partno is None:
        return
    if do_run_build_script:
        return
    if cached:
        return
    assert args.passphrase is not None

    with complete_step("Setting up LUKS on temporary data partition…"):
        luks_format(partition(loopdev, args.tmp_partno), args.passphrase)


@contextlib.contextmanager
def luks_open(dev: Path, passphrase: Dict[str, str], partition: str) -> Generator[Path, None, None]:
    name = str(uuid.uuid4())
    # FIXME: partition is only used in messages, rename it?

    with complete_step(f"Setting up LUKS on {partition}…"):
        if passphrase["type"] == "stdin":
            passphrase_content = (passphrase["content"] + "\n").encode("utf-8")
            run(["cryptsetup", "open", "--type", "luks", dev, name], input=passphrase_content)
        else:
            assert passphrase["type"] == "file"
            run(["cryptsetup", "--key-file", passphrase["content"], "open", "--type", "luks", dev, name])

    path = Path("/dev/mapper", name)

    try:
        yield path
    finally:
        with complete_step(f"Closing LUKS {partition}"):
            run(["cryptsetup", "close", path])


def luks_setup_root(
    args: CommandLineArguments, loopdev: Path, do_run_build_script: bool, inserting_generated_root: bool = False
) -> ContextManager[Optional[Path]]:
    if args.encrypt != "all":
        return contextlib.nullcontext()
    if args.root_partno is None:
        return contextlib.nullcontext()
    if is_generated_root(args) and not inserting_generated_root:
        return contextlib.nullcontext()
    if do_run_build_script:
        return contextlib.nullcontext()
    assert args.passphrase is not None

    return luks_open(partition(loopdev, args.root_partno), args.passphrase, "root partition")


def luks_setup_home(
    args: CommandLineArguments, loopdev: Path, do_run_build_script: bool
) -> ContextManager[Optional[Path]]:
    if args.encrypt is None:
        return contextlib.nullcontext()
    if args.home_partno is None:
        return contextlib.nullcontext()
    if do_run_build_script:
        return contextlib.nullcontext()
    assert args.passphrase is not None

    return luks_open(partition(loopdev, args.home_partno), args.passphrase, "home partition")


def luks_setup_srv(
    args: CommandLineArguments, loopdev: Path, do_run_build_script: bool
) -> ContextManager[Optional[Path]]:
    if args.encrypt is None:
        return contextlib.nullcontext()
    if args.srv_partno is None:
        return contextlib.nullcontext()
    if do_run_build_script:
        return contextlib.nullcontext()
    assert args.passphrase is not None

    return luks_open(partition(loopdev, args.srv_partno), args.passphrase, "server data partition")


def luks_setup_var(
    args: CommandLineArguments, loopdev: Path, do_run_build_script: bool
) -> ContextManager[Optional[Path]]:
    if args.encrypt is None:
        return contextlib.nullcontext()
    if args.var_partno is None:
        return contextlib.nullcontext()
    if do_run_build_script:
        return contextlib.nullcontext()
    assert args.passphrase is not None

    return luks_open(partition(loopdev, args.var_partno), args.passphrase, "variable data partition")


def luks_setup_tmp(
    args: CommandLineArguments, loopdev: Path, do_run_build_script: bool
) -> ContextManager[Optional[Path]]:
    if args.encrypt is None:
        return contextlib.nullcontext()
    if args.tmp_partno is None:
        return contextlib.nullcontext()
    if do_run_build_script:
        return contextlib.nullcontext()
    assert args.passphrase is not None

    return luks_open(partition(loopdev, args.tmp_partno), args.passphrase, "temporary data partition")


class LuksSetupOutput(NamedTuple):
    root: Optional[Path]
    home: Optional[Path]
    srv: Optional[Path]
    var: Optional[Path]
    tmp: Optional[Path]

    @classmethod
    def empty(cls) -> LuksSetupOutput:
        return cls(None, None, None, None, None)

    def without_generated_root(self, args: CommandLineArguments) -> LuksSetupOutput:
        "A copy of self with .root optionally supressed"
        return LuksSetupOutput(
            None if is_generated_root(args) else self.root,
            *self[1:],
        )


@contextlib.contextmanager
def luks_setup_all(
    args: CommandLineArguments, loopdev: Optional[Path], do_run_build_script: bool
) -> Generator[LuksSetupOutput, None, None]:
    if not args.output_format.is_disk():
        yield LuksSetupOutput.empty()
        return

    assert loopdev is not None

    with luks_setup_root(args, loopdev, do_run_build_script) as root, \
         luks_setup_home(args, loopdev, do_run_build_script) as home, \
         luks_setup_srv(args, loopdev, do_run_build_script) as srv, \
         luks_setup_var(args, loopdev, do_run_build_script) as var, \
         luks_setup_tmp(args, loopdev, do_run_build_script) as tmp:

        yield LuksSetupOutput(
            optional_partition(loopdev, args.root_partno) if root is None else root,
            optional_partition(loopdev, args.home_partno) if home is None else home,
            optional_partition(loopdev, args.srv_partno) if srv is None else srv,
            optional_partition(loopdev, args.var_partno) if var is None else var,
            optional_partition(loopdev, args.tmp_partno) if tmp is None else tmp,
        )


def prepare_root(args: CommandLineArguments, dev: Optional[Path], cached: bool) -> None:
    if dev is None:
        return
    if is_generated_root(args):
        return
    if cached:
        return

    label, path = ("usr", "/usr") if args.usr_only else ("root", "/")
    with complete_step(f"Formatting {label} partition…"):
        mkfs_generic(args, label, path, dev)


def prepare_home(args: CommandLineArguments, dev: Optional[Path], cached: bool) -> None:
    if dev is None:
        return
    if cached:
        return

    with complete_step("Formatting home partition…"):
        mkfs_generic(args, "home", "/home", dev)


def prepare_srv(args: CommandLineArguments, dev: Optional[Path], cached: bool) -> None:
    if dev is None:
        return
    if cached:
        return

    with complete_step("Formatting server data partition…"):
        mkfs_generic(args, "srv", "/srv", dev)


def prepare_var(args: CommandLineArguments, dev: Optional[Path], cached: bool) -> None:
    if dev is None:
        return
    if cached:
        return

    with complete_step("Formatting variable data partition…"):
        mkfs_generic(args, "var", "/var", dev)


def prepare_tmp(args: CommandLineArguments, dev: Optional[Path], cached: bool) -> None:
    if dev is None:
        return
    if cached:
        return

    with complete_step("Formatting temporary data partition…"):
        mkfs_generic(args, "tmp", "/var/tmp", dev)


def mount_loop(args: CommandLineArguments, dev: Path, where: Path, read_only: bool = False) -> None:
    os.makedirs(where, 0o755, True)

    options = []
    if not args.output_format.is_squashfs():
        options += ["discard"]

    compress = should_compress_fs(args)
    if compress and args.output_format == OutputFormat.gpt_btrfs and where.name not in {"efi", "boot"}:
        options += ["compress" if compress is True else f"compress={compress}"]

    if read_only:
        options += ["ro"]

    cmd: List[PathString] = ["mount", "-n", dev, where]
    if options:
        cmd += ["-o", ",".join(options)]

    run(cmd)


def mount_bind(what: Path, where: Optional[Path] = None) -> Path:
    if where is None:
        where = what

    os.makedirs(what, 0o755, True)
    os.makedirs(where, 0o755, True)
    run(["mount", "--bind", what, where])
    return where


def mount_tmpfs(where: Path) -> None:
    os.makedirs(where, 0o755, True)
    run(["mount", "tmpfs", "-t", "tmpfs", where])


@contextlib.contextmanager
def mount_image(
    args: CommandLineArguments,
    root: Path,
    loopdev: Optional[Path],
    image: LuksSetupOutput,
    root_read_only: bool = False,
) -> Generator[None, None, None]:
    with complete_step("Mounting image…"):

        if image.root is not None:
            if args.usr_only:
                # In UsrOnly mode let's have a bind mount at the top so that umount --recursive works nicely later
                mount_bind(root)
                mount_loop(args, image.root, root / "usr", root_read_only)
            else:
                mount_loop(args, image.root, root, root_read_only)
        else:
            # always have a root of the tree as a mount point so we can
            # recursively unmount anything that ends up mounted there
            mount_bind(root, root)

        if image.home is not None:
            mount_loop(args, image.home, root / "home")

        if image.srv is not None:
            mount_loop(args, image.srv, root / "srv")

        if image.var is not None:
            mount_loop(args, image.var, root / "var")

        if image.tmp is not None:
            mount_loop(args, image.tmp, root / "var/tmp")

        if args.esp_partno is not None and loopdev is not None:
            mount_loop(args, partition(loopdev, args.esp_partno), root / "efi")

        if args.xbootldr_partno is not None and loopdev is not None:
            mount_loop(args, partition(loopdev, args.xbootldr_partno), root / "boot")

        # Make sure /tmp and /run are not part of the image
        mount_tmpfs(root / "run")
        mount_tmpfs(root / "tmp")

    try:
        yield
    finally:
        with complete_step("Unmounting image"):
            umount(root)


def install_etc_hostname(args: CommandLineArguments, root: Path, cached: bool) -> None:
    if cached:
        return

    etc_hostname = root / "etc/hostname"

    # Always unlink first, so that we don't get in trouble due to a
    # symlink or suchlike. Also if no hostname is configured we really
    # don't want the file to exist, so that systemd's implicit
    # hostname logic can take effect.
    try:
        os.unlink(etc_hostname)
    except FileNotFoundError:
        pass

    if args.hostname:
        with complete_step("Assigning hostname"):
            etc_hostname.write_text(args.hostname + "\n")


@contextlib.contextmanager
def mount_api_vfs(args: CommandLineArguments, root: Path) -> Generator[None, None, None]:
    subdirs = ("proc", "dev", "sys")

    with complete_step("Mounting API VFS"):
        for subdir in subdirs:
            mount_bind(Path("/") / subdir, root / subdir)
    try:
        yield
    finally:
        with complete_step("Unmounting API VFS"):
            for subdir in subdirs:
                umount(root / subdir)


@contextlib.contextmanager
def mount_cache(args: CommandLineArguments, root: Path) -> Generator[None, None, None]:
    if args.cache_path is None:
        yield
        return

    caches = []

    # We can't do this in mount_image() yet, as /var itself might have to be created as a subvolume first
    with complete_step("Mounting Package Cache"):
        if args.distribution in (Distribution.fedora, Distribution.mageia, Distribution.openmandriva):
            caches = [mount_bind(args.cache_path, root / "var/cache/dnf")]
        elif args.distribution in (
            Distribution.centos,
            Distribution.centos_epel,
            Distribution.rocky,
            Distribution.rocky_epel,
            Distribution.alma,
            Distribution.alma_epel,
        ):
            # We mount both the YUM and the DNF cache in this case, as
            # YUM might just be redirected to DNF even if we invoke
            # the former
            caches = [
                mount_bind(args.cache_path / "yum", root / "var/cache/yum"),
                mount_bind(args.cache_path / "dnf", root / "var/cache/dnf"),
            ]
        elif args.distribution in (Distribution.debian, Distribution.ubuntu):
            caches = [mount_bind(args.cache_path, root / "var/cache/apt/archives")]
        elif args.distribution == Distribution.arch:
            caches = [mount_bind(args.cache_path, root / "var/cache/pacman/pkg")]
        elif args.distribution == Distribution.opensuse:
            caches = [mount_bind(args.cache_path, root / "var/cache/zypp/packages")]
        elif args.distribution == Distribution.photon:
            caches = [mount_bind(args.cache_path / "tdnf", root / "var/cache/tdnf")]
    try:
        yield
    finally:
        with complete_step("Unmounting Package Cache"):
            for d in caches:  # NOQA: E501
                umount(d)


def umount(where: Path) -> None:
    run(["umount", "--recursive", "-n", where])


def configure_dracut(args: CommandLineArguments, root: Path) -> None:
    dracut_dir = root / "etc/dracut.conf.d"
    dracut_dir.mkdir(mode=0o755)

    dracut_dir.joinpath('30-mkosi-hostonly.conf').write_text(
        f'hostonly={yes_no(args.hostonly_initrd)}\n'
        'hostonly_default_device=no\n'
    )

    dracut_dir.joinpath("30-mkosi-qemu.conf").write_text('add_dracutmodules+=" qemu "\n')

    with dracut_dir.joinpath("30-mkosi-systemd-extras.conf").open("w") as f:
        for extra in DRACUT_SYSTEMD_EXTRAS:
            f.write(f'install_optional_items+=" {extra} "\n')

    if args.hostonly_initrd:
        dracut_dir.joinpath("30-mkosi-filesystem.conf").write_text(
            f'filesystems+=" {(args.output_format.needed_kernel_module())} "\n'
        )

    # These distros need uefi_stub configured explicitly for dracut to find the systemd-boot uefi stub.
    if args.esp_partno is not None and args.distribution in (
        Distribution.ubuntu,
        Distribution.debian,
        Distribution.mageia,
        Distribution.openmandriva,
    ):
        dracut_dir.joinpath("30-mkosi-uefi-stub.conf").write_text(
            "uefi_stub=/usr/lib/systemd/boot/efi/linuxx64.efi.stub\n"
        )

    # efivarfs must be present in order to GPT root discovery work
    if args.esp_partno is not None:
        dracut_dir.joinpath("30-mkosi-efivarfs.conf").write_text(
            '[[ $(modinfo -k "$kernel" -F filename efivarfs 2>/dev/null) == /* ]] && add_drivers+=" efivarfs "\n'
        )


def prepare_tree_root(args: CommandLineArguments, root: Path) -> None:
    if args.output_format == OutputFormat.subvolume and not is_generated_root(args):
        with complete_step("Setting up OS tree root…"):
            btrfs_subvol_create(root)


def root_home(args: CommandLineArguments, root: Path) -> Path:

    # If UsrOnly= is turned on the /root/ directory (i.e. the root
    # user's home directory) is not persistent (after all everything
    # outside of /usr/ is not around). In that case let's mount it in
    # from an external place, so that we can have persistency. It is
    # after all where we place our build sources and suchlike.

    if args.usr_only:
        return workspace(root) / "home-root"

    return root / "root"


def prepare_tree(args: CommandLineArguments, root: Path, do_run_build_script: bool, cached: bool) -> None:
    if cached:
        return

    with complete_step("Setting up basic OS tree…"):
        if args.output_format in (OutputFormat.subvolume, OutputFormat.gpt_btrfs) and not is_generated_root(args):
            btrfs_subvol_create(root / "home")
            btrfs_subvol_create(root / "srv")
            btrfs_subvol_create(root / "var")
            btrfs_subvol_create(root / "var/tmp", 0o1777)
            root.joinpath("var/lib").mkdir()
            btrfs_subvol_create(root / "var/lib/machines", 0o700)

        # We need an initialized machine ID for the build & boot logic to work
        root.joinpath("etc").mkdir(mode=0o755, exist_ok=True)
        root.joinpath("etc/machine-id").write_text(f"{args.machine_id}\n")

        if not do_run_build_script and args.bootable:
            if args.xbootldr_partno is not None:
                # Create directories for kernels and entries if this is enabled
                root.joinpath("boot/EFI").mkdir(mode=0o700)
                root.joinpath("boot/EFI/Linux").mkdir(mode=0o700)
                root.joinpath("boot/loader").mkdir(mode=0o700)
                root.joinpath("boot/loader/entries").mkdir(mode=0o700)
                root.joinpath("boot", args.machine_id).mkdir(mode=0o700)
            else:
                # If this is not enabled, let's create an empty directory on /boot
                root.joinpath("boot").mkdir(mode=0o700)

            if args.esp_partno is not None:
                root.joinpath("efi/EFI").mkdir(mode=0o700)
                root.joinpath("efi/EFI/BOOT").mkdir(mode=0o700)
                root.joinpath("efi/EFI/systemd").mkdir(mode=0o700)
                root.joinpath("efi/loader").mkdir(mode=0o700)

                if args.xbootldr_partno is None:
                    # Create directories for kernels and entries, unless the XBOOTLDR partition is turned on
                    root.joinpath("efi/EFI/Linux").mkdir(mode=0o700)
                    root.joinpath("efi/loader/entries").mkdir(mode=0o700)
                    root.joinpath("efi", args.machine_id).mkdir(mode=0o700)

                    # Create some compatibility symlinks in /boot in case that is not set up otherwise
                    root.joinpath("boot/efi").symlink_to("../efi")
                    root.joinpath("boot/loader").symlink_to("../efi/loader")
                    root.joinpath("boot", args.machine_id).symlink_to(f"../efi/{args.machine_id}")

            root.joinpath("etc/kernel").mkdir(mode=0o755)

            root.joinpath("etc/kernel/cmdline").write_text(" ".join(args.kernel_command_line) + "\n")

        if do_run_build_script or args.ssh:
            root_home(args, root).mkdir(mode=0o750)

        if args.ssh and not do_run_build_script:
            root_home(args, root).joinpath(".ssh").mkdir(mode=0o700)

        if do_run_build_script:
            root_home(args, root).joinpath("dest").mkdir(mode=0o755)

            if args.include_dir is not None:
                root.joinpath("usr").mkdir(mode=0o755)
                root.joinpath("usr/include").mkdir(mode=0o755)

            if args.build_dir is not None:
                root_home(args, root).joinpath("build").mkdir(0o755)

        if args.network_veth and not do_run_build_script:
            root.joinpath("etc/systemd").mkdir(mode=0o755)
            root.joinpath("etc/systemd/network").mkdir(mode=0o755)


def disable_pam_securetty(root: Path) -> None:
    def _rm_securetty(line: str) -> str:
        if "pam_securetty.so" in line:
            return ""
        return line

    patch_file(root / "etc/pam.d/login", _rm_securetty)


def url_exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD")
    try:
        if urllib.request.urlopen(req):
            return True
    except Exception:
        pass
    return False


def make_executable(path: Path) -> None:
    st = path.stat()
    os.chmod(path, st.st_mode | stat.S_IEXEC)


def disable_kernel_install(args: CommandLineArguments, root: Path) -> None:
    # Let's disable the automatic kernel installation done by the kernel RPMs. After all, we want to built
    # our own unified kernels that include the root hash in the kernel command line and can be signed as a
    # single EFI executable. Since the root hash is only known when the root file system is finalized we turn
    # off any kernel installation beforehand.
    #
    # For BIOS mode, we don't have that option, so do not mask the units.
    if not args.bootable or args.bios_partno is not None or not args.with_unified_kernel_images:
        return

    for subdir in ("etc", "etc/kernel", "etc/kernel/install.d"):
        root.joinpath(subdir).mkdir(mode=0o755, exist_ok=True)

    for f in ("50-dracut.install", "51-dracut-rescue.install", "90-loaderentry.install"):
        root.joinpath("etc/kernel/install.d", f).symlink_to("/dev/null")


def reenable_kernel_install(args: CommandLineArguments, root: Path) -> None:
    if not args.bootable or args.bios_partno is not None or not args.with_unified_kernel_images:
        return

    write_resource(
        root / "etc/kernel/install.d/50-mkosi-dracut-unified-kernel.install",
        "mkosi.resources",
        "dracut_unified_kernel_install.sh",
        executable=True,
    )


def add_packages(
    args: CommandLineArguments, packages: Set[str], *names: str, conditional: Optional[str] = None
) -> None:
    """Add packages in @names to @packages, if enabled by --base-packages.

    If @conditional is specifed, rpm-specific syntax for boolean
    dependencies will be used to include @names if @conditional is
    satisfied.
    """
    assert args.base_packages is True or args.base_packages is False or args.base_packages == "conditional"

    if args.base_packages is True or (args.base_packages == "conditional" and conditional):
        for name in names:
            packages.add(f"({name} if {conditional})" if conditional else name)


def sort_packages(packages: Set[str]) -> List[str]:
    """Sorts packages: normal first, paths second, conditional third"""

    m = {"(": 2, "/": 1}
    sort = lambda name: (m.get(name[0], 0), name)
    return sorted(packages, key=sort)


def make_rpm_list(args: CommandLineArguments, packages: Set[str], do_run_build_script: bool) -> Set[str]:
    packages = packages.copy()

    if args.bootable:
        # Temporary hack: dracut only adds crypto support to the initrd, if the cryptsetup binary is installed
        if args.encrypt or args.verity:
            add_packages(args, packages, "cryptsetup", conditional="dracut")

        if args.output_format == OutputFormat.gpt_ext4:
            add_packages(args, packages, "e2fsprogs")

        if args.output_format == OutputFormat.gpt_xfs:
            add_packages(args, packages, "xfsprogs")

        if args.output_format == OutputFormat.gpt_btrfs:
            add_packages(args, packages, "btrfs-progs")

        if args.bios_partno:
            if args.distribution in (Distribution.mageia, Distribution.openmandriva):
                add_packages(args, packages, "grub2")
            else:
                add_packages(args, packages, "grub2-pc")

    if not do_run_build_script and args.ssh:
        add_packages(args, packages, "openssh-server")

    return packages


def clean_dnf_metadata(root: Path, always: bool) -> None:
    """Remove dnf metadata if /bin/dnf is not present in the image

    If dnf is not installed, there doesn't seem to be much use in
    keeping the dnf metadata, since it's not usable from within the
    image anyway.
    """
    paths = [
        root / "var/lib/dnf",
        *root.glob("var/log/dnf.*"),
        *root.glob("var/log/hawkey.*"),
        root / "var/cache/dnf",
    ]

    cond = always or not os.access(root / "bin/dnf", os.F_OK, follow_symlinks=False)

    if not cond or not any(path.exists() for path in paths):
        return

    with complete_step("Cleaning dnf metadata…"):
        for path in paths:
            unlink_try_hard(path)


def clean_yum_metadata(root: Path, always: bool) -> None:
    """Remove yum metadata if /bin/yum is not present in the image"""
    paths = [
        root / "var/lib/yum",
        *root.glob("var/log/yum.*"),
        root / "var/cache/yum",
    ]

    cond = always or not os.access(root / "bin/yum", os.F_OK, follow_symlinks=False)

    if not cond or not any(path.exists() for path in paths):
        return

    with complete_step("Cleaning yum metadata…"):
        for path in paths:
            unlink_try_hard(path)


def clean_rpm_metadata(root: Path, always: bool) -> None:
    """Remove rpm metadata if /bin/rpm is not present in the image"""
    path = root / "var/lib/rpm"

    cond = always or not os.access(root / "bin/rpm", os.F_OK, follow_symlinks=False)

    if not cond or not path.exists():
        return

    with complete_step("Cleaning rpm metadata…"):
        unlink_try_hard(path)


def clean_tdnf_metadata(root: Path, always: bool) -> None:
    """Remove tdnf metadata if /bin/tdnf is not present in the image"""
    paths = [
        *root.glob("var/log/tdnf.*"),
        root / "var/cache/tdnf",
    ]

    cond = always or not os.access(root / "usr/bin/tdnf", os.F_OK, follow_symlinks=False)

    if not cond or not any(path.exists() for path in paths):
        return

    with complete_step("Cleaning tdnf metadata…"):
        for path in paths:
            unlink_try_hard(path)


def clean_apt_metadata(root: Path, always: bool) -> None:
    """Remove apt metadata if /usr/bin/apt is not present in the image"""
    paths = [
        root / "var/lib/apt",
        root / "var/log/apt",
        root / "var/cache/apt",
    ]

    cond = always or not os.access(root / "usr/bin/apt", os.F_OK, follow_symlinks=False)

    if not cond or not any(path.exists() for path in paths):
        return

    with complete_step("Cleaning apt metadata…"):
        for path in paths:
            unlink_try_hard(path)


def clean_dpkg_metadata(root: Path, always: bool) -> None:
    """Remove dpkg metadata if /usr/bin/dpkg is not present in the image"""
    paths = [
        root / "var/lib/dpkg",
        root / "var/log/dpkg.log",
    ]

    cond = always or not os.access(root / "usr/bin/dpkg", os.F_OK, follow_symlinks=False)

    if not cond or not any(path.exists() for path in paths):
        return

    with complete_step("Cleaning dpkg metadata…"):
        for path in paths:
            unlink_try_hard(path)


def clean_package_manager_metadata(args: CommandLineArguments, root: Path) -> None:
    """Remove package manager metadata

    Try them all regardless of the distro: metadata is only removed if the
    package manager is present in the image.
    """

    assert args.clean_package_metadata in (False, True, 'auto')
    if args.clean_package_metadata is False:
        return

    # we try then all: metadata will only be touched if any of them are in the
    # final image
    clean_dnf_metadata(root, always=args.clean_package_metadata is True)
    clean_yum_metadata(root, always=args.clean_package_metadata is True)
    clean_rpm_metadata(root, always=args.clean_package_metadata is True)
    clean_tdnf_metadata(root, always=args.clean_package_metadata is True)
    clean_apt_metadata(root, always=args.clean_package_metadata is True)
    clean_dpkg_metadata(root, always=args.clean_package_metadata is True)
    # FIXME: implement cleanup for other package managers


def remove_files(args: CommandLineArguments, root: Path) -> None:
    """Remove files based on user-specified patterns"""

    if not args.remove_files:
        return

    with complete_step("Removing files…"):
        # Note: Path('/foo') / '/bar' == '/bar'. We need to strip the slash.
        # https://bugs.python.org/issue44452
        paths = [root / str(p).lstrip("/") for p in args.remove_files]
        remove_glob(*paths)


def invoke_dnf(
    args: CommandLineArguments, root: Path, repositories: List[str], packages: Set[str], do_run_build_script: bool
) -> None:
    repos = [f"--enablerepo={repo}" for repo in repositories]
    config_file = workspace(root) / "dnf.conf"
    packages = make_rpm_list(args, packages, do_run_build_script)

    cmdline = [
        "dnf",
        "-y",
        f"--config={config_file}",
        "--best",
        "--allowerasing",
        f"--releasever={args.release}",
        f"--installroot={root}",
        "--disablerepo=*",
        *repos,
        "--setopt=keepcache=1",
        "--setopt=install_weak_deps=0",
    ]

    if args.with_network == "never":
        cmdline += ["-C"]

    if args.architecture is not None:
        cmdline += [f"--forcearch={args.architecture}"]

    if not args.with_docs:
        cmdline += ["--nodocs"]

    cmdline += ["install", *sort_packages(packages)]

    with mount_api_vfs(args, root):
        run(cmdline)


def invoke_tdnf(
    args: CommandLineArguments,
    root: Path,
    repositories: List[str],
    packages: Set[str],
    gpgcheck: bool,
    do_run_build_script: bool,
) -> None:
    repos = [f"--enablerepo={repo}" for repo in repositories]
    config_file = workspace(root) / "dnf.conf"
    packages = make_rpm_list(args, packages, do_run_build_script)

    cmdline = [
        "tdnf",
        "-y",
        f"--config={config_file}",
        f"--releasever={args.release}",
        f"--installroot={root}",
        "--disablerepo=*",
        *repos,
    ]

    if not gpgcheck:
        cmdline += ["--nogpgcheck"]

    cmdline += ["install", *sort_packages(packages)]

    with mount_api_vfs(args, root):
        run(cmdline)


class Repo(NamedTuple):
    id: str
    name: str
    url: str
    gpgpath: Path
    gpgurl: Optional[str] = None


def setup_dnf(args: CommandLineArguments, root: Path, repos: Sequence[Repo] = ()) -> None:
    gpgcheck = True

    repo_file = workspace(root) / "temp.repo"
    with repo_file.open("w") as f:
        for repo in repos:
            gpgkey: Optional[str] = None

            if repo.gpgpath.exists():
                gpgkey = f"file://{repo.gpgpath}"
            elif repo.gpgurl:
                gpgkey = repo.gpgurl
            else:
                warn(f"GPG key not found at {repo.gpgpath}. Not checking GPG signatures.")
                gpgcheck = False

            f.write(
                dedent(
                    f"""\
                    [{repo.id}]
                    name={repo.name}
                    {repo.url}
                    gpgkey={gpgkey or ''}
                    """
                )
            )

    if args.use_host_repositories:
        default_repos  = ""
    else:
        default_repos  = f"{'repodir' if args.distribution == Distribution.photon else 'reposdir'}={workspace(root)}"

    config_file = workspace(root) / "dnf.conf"
    config_file.write_text(
        dedent(
            f"""\
            [main]
            gpgcheck={'1' if gpgcheck else '0'}
            {default_repos }
            """
        )
    )


@complete_step("Installing Photon…")
def install_photon(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    release_url = "baseurl=https://packages.vmware.com/photon/$releasever/photon_release_$releasever_$basearch"
    updates_url = "baseurl=https://packages.vmware.com/photon/$releasever/photon_updates_$releasever_$basearch"
    gpgpath = Path("/etc/pki/rpm-gpg/VMWARE-RPM-GPG-KEY")

    setup_dnf(
        args,
        root,
        repos=[
            Repo("photon", f"VMware Photon OS {args.release} Release", release_url, gpgpath),
            Repo("photon-updates", f"VMware Photon OS {args.release} Updates", updates_url, gpgpath),
        ],
    )

    packages = {*args.packages}
    add_packages(args, packages, "minimal")
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "linux", "initramfs")

    invoke_tdnf(
        args,
        root,
        args.repositories or ["photon", "photon-updates"],
        packages,
        gpgpath.exists(),
        do_run_build_script,
    )


@complete_step("Installing Clear Linux…")
def install_clear(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    if args.release == "latest":
        release = "clear"
    else:
        release = "clear/" + args.release

    packages = {*args.packages}
    add_packages(args, packages, "os-core-plus")
    if do_run_build_script:
        packages.update(args.build_packages)
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "kernel-native")
    if not do_run_build_script and args.ssh:
        add_packages(args, packages, "openssh-server")

    swupd_extract = shutil.which("swupd-extract")

    if swupd_extract is None:
        die(
            dedent(
                """
                Couldn't find swupd-extract program, download (or update it) it using:

                  go get -u github.com/clearlinux/mixer-tools/swupd-extract

                and it will be installed by default in ~/go/bin/swupd-extract. Also
                ensure that you have openssl program in your system.
                """
            )
        )

    cmdline: List[PathString] = [swupd_extract, "-output", root]
    if args.cache_path:
        cmdline += ["-state", args.cache_path]
    cmdline += [release, *sort_packages(packages)]

    run(cmdline)

    root.joinpath("etc/resolv.conf").symlink_to("../run/systemd/resolve/resolv.conf")

    # Clear Linux doesn't have a /etc/shadow at install time, it gets
    # created when the root first login. To set the password via
    # mkosi, create one.
    if not do_run_build_script and args.password is not None:
        shadow_file = root / "etc/shadow"
        shadow_file.write_text("root::::::::\n")
        shadow_file.chmod(0o400)
        # Password is already empty for root, so no need to reset it later.
        if args.password == "":
            args.password = None


@complete_step("Installing Fedora Linux…")
def install_fedora(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    if args.release == "rawhide":
        last = list(FEDORA_KEYS_MAP)[-1]
        warn(f"Assuming rawhide is version {last} — " + "You may specify otherwise with --release=rawhide-<version>")
        args.releasever = last
    elif args.release.startswith("rawhide-"):
        args.release, args.releasever = args.release.split("-")
        MkosiPrinter.info(f"Fedora rawhide — release version: {args.releasever}")
    else:
        args.releasever = args.release

    arch = args.architecture or platform.machine()

    if args.mirror:
        baseurl = urllib.parse.urljoin(args.mirror, f"releases/{args.release}/Everything/$basearch/os/")
        media = urllib.parse.urljoin(baseurl.replace("$basearch", arch), "media.repo")
        if not url_exists(media):
            baseurl = urllib.parse.urljoin(args.mirror, f"development/{args.release}/Everything/$basearch/os/")

        release_url = f"baseurl={baseurl}"
        updates_url = f"baseurl={args.mirror}/updates/{args.release}/Everything/$basearch/"
    else:
        release_url = f"metalink=https://mirrors.fedoraproject.org/metalink?repo=fedora-{args.release}&arch=$basearch"
        updates_url = (
            "metalink=https://mirrors.fedoraproject.org/metalink?"
            f"repo=updates-released-f{args.release}&arch=$basearch"
        )

    if args.releasever in FEDORA_KEYS_MAP:
        # The website uses short identifiers: https://pagure.io/fedora-web/websites/issue/196
        shortid = FEDORA_KEYS_MAP[args.releasever][-8:]
        gpgid = f"keys/{shortid}.txt"
    else:
        gpgid = "fedora.gpg"

    gpgpath = Path(f"/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-{args.releasever}-{arch}")
    gpgurl = urllib.parse.urljoin("https://getfedora.org/static/", gpgid)

    setup_dnf(
        args,
        root,
        repos=[
            Repo("fedora", f"Fedora {args.release.capitalize()} - base", release_url, gpgpath, gpgurl),
            Repo("updates", f"Fedora {args.release.capitalize()} - updates", updates_url, gpgpath, gpgurl),
        ],
    )

    packages = {*args.packages}
    add_packages(args, packages, "fedora-release", "systemd")
    add_packages(args, packages, "glibc-minimal-langpack", conditional="glibc")
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "kernel-core", "kernel-modules", "binutils", "dracut")
        add_packages(args, packages, "systemd-udev", conditional="systemd")
        configure_dracut(args, root)
    if do_run_build_script:
        packages.update(args.build_packages)
    if not do_run_build_script and args.network_veth:
        add_packages(args, packages, "systemd-networkd", conditional="systemd")
    invoke_dnf(args, root, args.repositories or ["fedora", "updates"], packages, do_run_build_script)

    root.joinpath("etc/locale.conf").write_text("LANG=C.UTF-8\n")

    # FIXME: should this be conditionalized on args.with_docs like in install_debian_or_ubuntu()?
    #        But we set LANG=C.UTF-8 anyway.
    shutil.rmtree(root / "usr/share/locale", ignore_errors=True)


@complete_step("Installing Mageia…")
def install_mageia(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    if args.mirror:
        baseurl = f"{args.mirror}/distrib/{args.release}/x86_64/media/core/"
        release_url = f"baseurl={baseurl}/release/"
        updates_url = f"baseurl={baseurl}/updates/"
    else:
        baseurl = f"https://www.mageia.org/mirrorlist/?release={args.release}&arch=x86_64&section=core"
        release_url = f"mirrorlist={baseurl}&repo=release"
        updates_url = f"mirrorlist={baseurl}&repo=updates"

    gpgpath = Path("/etc/pki/rpm-gpg/RPM-GPG-KEY-Mageia")

    setup_dnf(
        args,
        root,
        repos=[
            Repo("mageia", f"Mageia {args.release} Core Release", release_url, gpgpath),
            Repo("updates", f"Mageia {args.release} Core Updates", updates_url, gpgpath),
        ],
    )

    packages = {*args.packages}
    add_packages(args, packages, "basesystem-minimal")
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "kernel-server-latest", "binutils", "dracut")
        configure_dracut(args, root)
        # Mageia ships /etc/50-mageia.conf that omits systemd from the initramfs and disables hostonly.
        # We override that again so our defaults get applied correctly on Mageia as well.
        root.joinpath("etc/dracut.conf.d/51-mkosi-override-mageia.conf").write_text(
            'hostonly=no\n'
            'omit_dracutmodules=""\n'
        )

    if do_run_build_script:
        packages.update(args.build_packages)
    invoke_dnf(args, root, args.repositories or ["mageia", "updates"], packages, do_run_build_script)

    disable_pam_securetty(root)


@complete_step("Installing OpenMandriva…")
def install_openmandriva(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    release = args.release.strip("'")
    arch = args.architecture or platform.machine()

    if release[0].isdigit():
        release_model = "rock"
    elif release == "cooker":
        release_model = "cooker"
    else:
        release_model = release

    if args.mirror:
        baseurl = f"{args.mirror}/{release_model}/repository/{arch}/main"
        release_url = f"baseurl={baseurl}/release/"
        updates_url = f"baseurl={baseurl}/updates/"
    else:
        baseurl = f"http://mirrors.openmandriva.org/mirrors.php?platform={release_model}&arch={arch}&repo=main"
        release_url = f"mirrorlist={baseurl}&release=release"
        updates_url = f"mirrorlist={baseurl}&release=updates"

    gpgpath = Path("/etc/pki/rpm-gpg/RPM-GPG-KEY-OpenMandriva")

    setup_dnf(
        args,
        root,
        repos=[
            Repo("openmandriva", f"OpenMandriva {release_model} Main", release_url, gpgpath),
            Repo("updates", f"OpenMandriva {release_model} Main Updates", updates_url, gpgpath),
        ],
    )

    packages = {*args.packages}
    # well we may use basesystem here, but that pulls lot of stuff
    add_packages(args, packages, "basesystem-minimal", "systemd")
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "systemd-boot", "systemd-cryptsetup", conditional="systemd")
        add_packages(args, packages, "kernel-release-server", "binutils", "dracut", "timezone")
        configure_dracut(args, root)
    if args.network_veth:
        add_packages(args, packages, "systemd-networkd", conditional="systemd")

    if do_run_build_script:
        packages.update(args.build_packages)
    invoke_dnf(args, root, args.repositories or ["openmandriva", "updates"], packages, do_run_build_script)

    disable_pam_securetty(root)


def invoke_yum(
    args: CommandLineArguments, root: Path, repositories: List[str], packages: Set[str], do_run_build_script: bool
) -> None:
    repos = ["--enablerepo=" + repo for repo in repositories]
    config_file = workspace(root) / "dnf.conf"
    packages = make_rpm_list(args, packages, do_run_build_script)

    cmdline = [
        "yum",
        "-y",
        f"--config={config_file}",
        f"--releasever={args.release}",
        f"--installroot={root}",
        "--disablerepo=*",
        *repos,
        "--setopt=keepcache=1",
    ]

    if args.architecture is not None:
        cmdline += [f"--forcearch={args.architecture}"]

    if not args.with_docs:
        cmdline += ["--setopt=tsflags=nodocs"]

    cmdline += ["install", *packages]

    with mount_api_vfs(args, root):
        run(cmdline)


def invoke_dnf_or_yum(
    args: CommandLineArguments, root: Path, repositories: List[str], packages: Set[str], do_run_build_script: bool
) -> None:
    if shutil.which("dnf") is None:
        invoke_yum(args, root, repositories, packages, do_run_build_script)
    else:
        invoke_dnf(args, root, repositories, packages, do_run_build_script)


def install_centos_old(args: CommandLineArguments, root: Path, epel_release: int) -> List[str]:
    # Repos for CentOS 7 and earlier

    gpgpath = Path(f"/etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-{args.release}")
    gpgurl = f"https://www.centos.org/keys/RPM-GPG-KEY-CentOS-{args.release}"
    epel_gpgpath = Path(f"/etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-{epel_release}")
    epel_gpgurl = f"https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-{epel_release}"

    if args.mirror:
        release_url = f"baseurl={args.mirror}/centos/{args.release}/os/x86_64"
        updates_url = f"baseurl={args.mirror}/centos/{args.release}/updates/x86_64/"
        extras_url = f"baseurl={args.mirror}/centos/{args.release}/extras/x86_64/"
        centosplus_url = f"baseurl={args.mirror}/centos/{args.release}/centosplus/x86_64/"
        epel_url = f"baseurl={args.mirror}/epel/{epel_release}/x86_64/"
    else:
        release_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=os"
        updates_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=updates"
        extras_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=extras"
        centosplus_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=centosplus"
        epel_url = f"mirrorlist=https://mirrors.fedoraproject.org/mirrorlist?repo=epel-{epel_release}&arch=x86_64"

    setup_dnf(
        args,
        root,
        repos=[
            Repo("base", f"CentOS-{args.release} - Base", release_url, gpgpath, gpgurl),
            Repo("updates", f"CentOS-{args.release} - Updates", updates_url, gpgpath, gpgurl),
            Repo("extras", f"CentOS-{args.release} - Extras", extras_url, gpgpath, gpgurl),
            Repo("centosplus", f"CentOS-{args.release} - Plus", centosplus_url, gpgpath, gpgurl),
            Repo(
                "epel",
                f"name=Extra Packages for Enterprise Linux {epel_release} - $basearch",
                epel_url,
                epel_gpgpath,
                epel_gpgurl,
            ),
        ],
    )

    return ["base", "updates", "extras", "centosplus"]


def install_rocky_repos(args: CommandLineArguments, root: Path, epel_release: int) -> List[str]:
    # Repos for Rocky Linux 8 and later
    gpgpath = Path("/etc/pki/rpm-gpg/RPM-GPG-KEY-rockyofficial")
    gpgurl = "https://download.rockylinux.org/pub/rocky/RPM-GPG-KEY-rockyofficial"
    epel_gpgpath = Path(f"/etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-{epel_release}")
    epel_gpgurl = f"https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-{epel_release}"

    if args.mirror:
        appstream_url = f"baseurl={args.mirror}/rocky/{args.release}/AppStream/x86_64/os"
        baseos_url = f"baseurl={args.mirror}/rocky/{args.release}/BaseOS/x86_64/os"
        extras_url = f"baseurl={args.mirror}/rocky/{args.release}/extras/x86_64/os"
        plus_url = f"baseurl={args.mirror}/rocky/{args.release}/plus/x86_64/os"
        epel_url = f"baseurl={args.mirror}/epel/{epel_release}/Everything/x86_64"
    else:
        appstream_url = (
            f"mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=x86_64&repo=AppStream-{args.release}"
        )
        baseos_url = f"mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=x86_64&repo=BaseOS-{args.release}"
        extras_url = f"mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=x86_64&repo=extras-{args.release}"
        plus_url = f"mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=x86_64&repo=rockyplus-{args.release}"
        epel_url = f"mirrorlist=https://mirrors.fedoraproject.org/mirrorlist?repo=epel-{epel_release}&arch=x86_64"

    setup_dnf(
        args,
        root,
        repos=[
            Repo("AppStream", f"Rocky-{args.release} - AppStream", appstream_url, gpgpath, gpgurl),
            Repo("BaseOS", f"Rocky-{args.release} - Base", baseos_url, gpgpath, gpgurl),
            Repo("extras", f"Rocky-{args.release} - Extras", extras_url, gpgpath, gpgurl),
            Repo("plus", f"Rocky-{args.release} - Plus", plus_url, gpgpath, gpgurl),
            Repo(
                "epel",
                f"name=Extra Packages for Enterprise Linux {epel_release} - $basearch",
                epel_url,
                epel_gpgpath,
                epel_gpgurl,
            ),
        ],
    )

    return ["AppStream", "BaseOS", "extras", "plus"]


def install_alma_repos(args: CommandLineArguments, root: Path, epel_release: int) -> List[str]:
    # Repos for Alma Linux 8 and later
    gpgpath = Path("/etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux")
    gpgurl = "https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux"
    epel_gpgpath = Path(f"/etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-{epel_release}")
    epel_gpgurl = f"https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-{epel_release}"

    if args.mirror:
        appstream_url = f"baseurl={args.mirror}/almalinux/{args.release}/AppStream/x86_64/os"
        baseos_url = f"baseurl={args.mirror}/almalinux/{args.release}/BaseOS/x86_64/os"
        extras_url = f"baseurl={args.mirror}/almalinux/{args.release}/extras/x86_64/os"
        powertools_url = f"baseurl={args.mirror}/almalinux/{args.release}/PowerTools/x86_64/os"
        ha_url = f"baseurl={args.mirror}/almalinux/{args.release}/HighAvailability/x86_64/os"
        epel_url = f"baseurl={args.mirror}/epel/{epel_release}/Everything/x86_64"
    else:
        appstream_url = (
            f"mirrorlist=https://mirrors.almalinux.org/mirrorlist/{args.release}/appstream"
        )
        baseos_url = f"mirrorlist=https://mirrors.almalinux.org/mirrorlist/{args.release}/baseos"
        extras_url = f"mirrorlist=https://mirrors.almalinux.org/mirrorlist/{args.release}/extras"
        powertools_url = f"mirrorlist=https://mirrors.almalinux.org/mirrorlist/{args.release}/powertools"
        ha_url = f"mirrorlist=https://mirrors.almalinux.org/mirrorlist/{args.release}/ha"
        epel_url = f"mirrorlist=https://mirrors.fedoraproject.org/mirrorlist?repo=epel-{epel_release}&arch=x86_64"

    setup_dnf(
        args,
        root,
        repos=[
            Repo("AppStream", f"AlmaLinux-{args.release} - AppStream", appstream_url, gpgpath, gpgurl),
            Repo("BaseOS", f"AlmaLinux-{args.release} - Base", baseos_url, gpgpath, gpgurl),
            Repo("extras", f"AlmaLinux-{args.release} - Extras", extras_url, gpgpath, gpgurl),
            Repo("Powertools", f"AlmaLinux-{args.release} - Powertools", powertools_url, gpgpath, gpgurl),
            Repo("HighAvailability", f"AlmaLinux-{args.release} - HighAvailability", ha_url, gpgpath, gpgurl),
            Repo(
                "epel",
                f"name=Extra Packages for Enterprise Linux {epel_release} - $basearch",
                epel_url,
                epel_gpgpath,
                epel_gpgurl,
            ),
        ],
    )

    return ["AppStream", "BaseOS", "extras", "Powertools", "HighAvailability"]

def install_centos_new(args: CommandLineArguments, root: Path, epel_release: int) -> List[str]:
    # Repos for CentOS 8 and later

    gpgpath = Path("/etc/pki/rpm-gpg/RPM-GPG-KEY-centosofficial")
    gpgurl = "https://www.centos.org/keys/RPM-GPG-KEY-CentOS-Official"
    epel_gpgpath = Path(f"/etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-{epel_release}")
    epel_gpgurl = f"https://dl.fedoraproject.org/pub/epel/RPM-GPG-KEY-EPEL-{epel_release}"

    if args.mirror:
        appstream_url = f"baseurl={args.mirror}/centos/{args.release}/AppStream/x86_64/os"
        baseos_url = f"baseurl={args.mirror}/centos/{args.release}/BaseOS/x86_64/os"
        extras_url = f"baseurl={args.mirror}/centos/{args.release}/extras/x86_64/os"
        centosplus_url = f"baseurl={args.mirror}/centos/{args.release}/centosplus/x86_64/os"
        epel_url = f"baseurl={args.mirror}/epel/{epel_release}/Everything/x86_64"
    else:
        appstream_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=AppStream"
        baseos_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=BaseOS"
        extras_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=extras"
        centosplus_url = f"mirrorlist=http://mirrorlist.centos.org/?release={args.release}&arch=x86_64&repo=centosplus"
        epel_url = f"mirrorlist=https://mirrors.fedoraproject.org/mirrorlist?repo=epel-{epel_release}&arch=x86_64"

    setup_dnf(
        args,
        root,
        repos=[
            Repo("AppStream", f"CentOS-{args.release} - AppStream", appstream_url, gpgpath, gpgurl),
            Repo("BaseOS", f"CentOS-{args.release} - Base", baseos_url, gpgpath, gpgurl),
            Repo("extras", f"CentOS-{args.release} - Extras", extras_url, gpgpath, gpgurl),
            Repo("centosplus", f"CentOS-{args.release} - Plus", centosplus_url, gpgpath, gpgurl),
            Repo(
                "epel",
                f"name=Extra Packages for Enterprise Linux {epel_release} - $basearch",
                epel_url,
                epel_gpgpath,
                epel_gpgurl,
            ),
        ],
    )

    return ["AppStream", "BaseOS", "extras", "centosplus"]


def is_older_than_centos8(release: str) -> bool:
    # CentOS 7 contains some very old versions of certain libraries
    # which require workarounds in different places.
    # Additionally the repositories have been changed between 7 and 8
    epel_release = release.split(".")[0]
    try:
        return int(epel_release) <= 7
    except ValueError:
        return False


@complete_step("Installing CentOS…")
def install_centos(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    old = is_older_than_centos8(args.release)
    epel_release = int(args.release.split(".")[0])

    if old:
        default_repos = install_centos_old(args, root, epel_release)
    else:
        default_repos = install_centos_new(args, root, epel_release)

    packages = {*args.packages}
    add_packages(args, packages, "centos-release", "systemd")
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "kernel", "dracut", "binutils")
        configure_dracut(args, root)
        if old:
            add_packages(
                args,
                packages,
                "grub2-efi",
                "grub2-tools",
                "grub2-efi-x64-modules",
                "shim-x64",
                "efibootmgr",
                "efivar-libs",
            )
        else:
            # this does not exist on CentOS 7
            add_packages(args, packages, "systemd-udev", conditional="systemd")

    if do_run_build_script:
        packages.update(args.build_packages)

    repos = args.repositories or default_repos

    if args.distribution == Distribution.centos_epel:
        repos += ["epel"]
        add_packages(args, packages, "epel-release")

    if do_run_build_script:
        packages.update(args.build_packages)

    if not do_run_build_script and args.distribution == Distribution.centos_epel and args.network_veth:
        add_packages(args, packages, "systemd-networkd", conditional="systemd")

    invoke_dnf_or_yum(args, root, repos, packages, do_run_build_script)


@complete_step("Installing Rocky Linux…")
def install_rocky(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    epel_release = int(args.release.split(".")[0])
    default_repos = install_rocky_repos(args, root, epel_release)

    packages = {*args.packages}
    add_packages(args, packages, "rocky-release", "systemd")
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "kernel", "dracut", "binutils")
        configure_dracut(args, root)
        add_packages(args, packages, "systemd-udev", conditional="systemd")

    if do_run_build_script:
        packages.update(args.build_packages)

    repos = args.repositories or default_repos

    if args.distribution == Distribution.rocky_epel:
        repos += ["epel"]
        add_packages(args, packages, "epel-release")

    if do_run_build_script:
        packages.update(args.build_packages)

    if not do_run_build_script and args.distribution == Distribution.rocky_epel and args.network_veth:
        add_packages(args, packages, "systemd-networkd", conditional="systemd")

    invoke_dnf_or_yum(args, root, repos, packages, do_run_build_script)



@complete_step("Installing Alma Linux…")
def install_alma(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    epel_release = int(args.release.split(".")[0])
    default_repos = install_alma_repos(args, root, epel_release)

    packages = {*args.packages}
    add_packages(args, packages, "almalinux-release", "systemd")
    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "kernel", "dracut", "binutils")
        configure_dracut(args, root)
        add_packages(args, packages, "systemd-udev", conditional="systemd")

    if do_run_build_script:
        packages.update(args.build_packages)

    repos = args.repositories or default_repos

    if args.distribution == Distribution.alma_epel:
        repos += ["epel"]
        add_packages(args, packages, "epel-release")

    if do_run_build_script:
        packages.update(args.build_packages)

    if not do_run_build_script and args.distribution == Distribution.alma_epel and args.network_veth:
        add_packages(args, packages, "systemd-networkd", conditional="systemd")

    invoke_dnf_or_yum(args, root, repos, packages, do_run_build_script)


def debootstrap_knows_arg(arg: str) -> bool:
    return bytes("invalid option", "UTF-8") not in run(["debootstrap", arg], stdout=PIPE, check=False).stdout


def install_debian_or_ubuntu(args: CommandLineArguments, root: Path, *, do_run_build_script: bool) -> None:
    repos = set(args.repositories) or {"main"}
    # Ubuntu needs the 'universe' repo to install 'dracut'
    if args.distribution == Distribution.ubuntu and args.bootable:
        repos.add("universe")

    cmdline: List[PathString] = [
        "debootstrap",
        "--variant=minbase",
        "--merged-usr",
        f"--components={','.join(repos)}",
    ]

    if args.architecture is not None:
        debarch = DEBIAN_ARCHITECTURES.get(args.architecture)
        cmdline += [f"--arch={debarch}"]

    # Let's use --no-check-valid-until only if debootstrap knows it
    if debootstrap_knows_arg("--no-check-valid-until"):
        cmdline += ["--no-check-valid-until"]

    # Either the image builds or it fails and we restart, we don't need safety fsyncs when bootstrapping
    # Add it before debootstrap, as the second stage already uses dpkg from the chroot
    dpkg_io_conf = root / "etc/dpkg/dpkg.cfg.d/unsafe_io"
    os.makedirs(dpkg_io_conf.parent, mode=0o755, exist_ok=True)
    dpkg_io_conf.write_text("force-unsafe-io\n")

    assert args.mirror is not None
    cmdline += [args.release, root, args.mirror]
    run(cmdline)

    # Install extra packages via the secondary APT run, because it is smarter and can deal better with any
    # conflicts. dbus and libpam-systemd are optional dependencies for systemd in debian so we include them
    # explicitly.
    extra_packages: Set[str] = set()
    add_packages(args, extra_packages, "systemd", "systemd-sysv", "dbus", "libpam-systemd")
    extra_packages.update(args.packages)

    if do_run_build_script:
        extra_packages.update(args.build_packages)

    if not do_run_build_script and args.bootable:
        add_packages(args, extra_packages, "dracut", "binutils")
        configure_dracut(args, root)

        if args.distribution == Distribution.ubuntu:
            add_packages(args, extra_packages, "linux-generic")
        else:
            add_packages(args, extra_packages, "linux-image-amd64")

        if args.bios_partno:
            add_packages(args, extra_packages, "grub-pc")

        if args.output_format == OutputFormat.gpt_btrfs:
            add_packages(args, extra_packages, "btrfs-progs")

    if not do_run_build_script and args.ssh:
        add_packages(args, extra_packages, "openssh-server")

    # Debian policy is to start daemons by default. The policy-rc.d script can be used choose which ones to
    # start. Let's install one that denies all daemon startups.
    # See https://people.debian.org/~hmh/invokerc.d-policyrc.d-specification.txt for more information.
    # Note: despite writing in /usr/sbin, this file is not shipped by the OS and instead should be managed by
    # the admin.
    policyrcd = root / "usr/sbin/policy-rc.d"
    policyrcd.write_text("#!/bin/sh\nexit 101\n")
    policyrcd.chmod(0o755)

    doc_paths = [
        "/usr/share/locale",
        "/usr/share/doc",
        "/usr/share/man",
        "/usr/share/groff",
        "/usr/share/info",
        "/usr/share/lintian",
        "/usr/share/linda",
    ]
    if not args.with_docs:
        # Remove documentation installed by debootstrap
        cmdline = ["/bin/rm", "-rf", *doc_paths]
        run_workspace_command(args, root, cmdline)
        # Create dpkg.cfg to ignore documentation on new packages
        dpkg_conf = root / "etc/dpkg/dpkg.cfg.d/01_nodoc"
        with dpkg_conf.open("w") as f:
            f.writelines(f"path-exclude {d}/*\n" for d in doc_paths)

    cmdline = ["/usr/bin/apt-get", "--assume-yes", "--no-install-recommends", "install", *extra_packages]
    env = {
        "DEBIAN_FRONTEND": "noninteractive",
        "DEBCONF_NONINTERACTIVE_SEEN": "true",
    }

    if not do_run_build_script and args.bootable and args.with_unified_kernel_images:
        # Disable dracut postinstall script for this apt-get run.
        env["INITRD"] = "No"

        if args.distribution == Distribution.debian and args.release == "unstable":
            # systemd-boot won't boot unified kernel images generated without a BUILD_ID or VERSION_ID in
            # /etc/os-release.
            with root.joinpath("etc/os-release").open("a") as f:
                f.write("BUILD_ID=unstable\n")

    run_workspace_command(args, root, cmdline, network=True, env=env)
    policyrcd.unlink()
    dpkg_io_conf.unlink()
    # Debian still has pam_securetty module enabled
    disable_pam_securetty(root)

    if args.distribution == Distribution.debian:
        # The default resolv.conf points to 127.0.0.1, and resolved is disabled
        root.joinpath("etc/resolv.conf").unlink()
        root.joinpath("etc/resolv.conf").symlink_to("../run/systemd/resolve/resolv.conf")
        run(["systemctl", "--root", root, "enable", "systemd-resolved"])


@complete_step("Installing Debian…")
def install_debian(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    install_debian_or_ubuntu(args, root, do_run_build_script=do_run_build_script)


@complete_step("Installing Ubuntu…")
def install_ubuntu(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    install_debian_or_ubuntu(args, root, do_run_build_script=do_run_build_script)


def run_pacman(root: Path, pacman_conf: Path, packages: Set[str]) -> None:
    try:
        run(["pacman-key", "--config", pacman_conf, "--init"])
        run(["pacman-key", "--config", pacman_conf, "--populate"])
        run(["pacman", "--config", pacman_conf, "--noconfirm", "-Sy", *sort_packages(packages)])
    finally:
        # Kill the gpg-agent started by pacman and pacman-key.
        run(["gpgconf", "--homedir", root / "etc/pacman.d/gnupg", "--kill", "all"])


def patch_locale_gen(args: CommandLineArguments, root: Path) -> None:
    # If /etc/locale.gen exists, uncomment the desired locale and leave the rest of the file untouched.
    # If it doesn’t exist, just write the desired locale in it.
    try:

        def _patch_line(line: str) -> str:
            if line.startswith("#en_US.UTF-8"):
                return line[1:]
            return line

        patch_file(root / "etc/locale.gen", _patch_line)

    except FileNotFoundError:
        root.joinpath("etc/locale.gen").write_text("en_US.UTF-8 UTF-8\n")


@complete_step("Installing Arch Linux…")
def install_arch(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    if args.release is not None:
        MkosiPrinter.info("Distribution release specification is not supported for Arch Linux, ignoring.")

    if args.mirror:
        if platform.machine() == "aarch64":
            server = f"Server = {args.mirror}/$arch/$repo"
        else:
            server = f"Server = {args.mirror}/$repo/os/$arch"
    else:
        # Instead of harcoding a single mirror, we retrieve a list of mirrors from Arch's mirrorlist
        # generator ordered by mirror score. This usually results in a solid mirror and also ensures that we
        # have fallback mirrors available if necessary. Finally, the mirrors will be more likely to be up to
        # date and we won't end up with a stable release that hardcodes a broken mirror.
        mirrorlist = workspace(root) / "mirrorlist"
        with urllib.request.urlopen(
            "https://www.archlinux.org/mirrorlist/?country=all&protocol=https&ip_version=4&use_mirror_status=on"
        ) as r:
            mirrors = r.readlines()
            uncommented = [line.decode("utf-8")[1:] for line in mirrors]
            with mirrorlist.open("w") as f:
                f.writelines(uncommented)
            server = f"Include = {mirrorlist}"

    # Create base layout for pacman and pacman-key
    os.makedirs(root / "var/lib/pacman", 0o755, exist_ok=True)
    os.makedirs(root / "etc/pacman.d/gnupg", 0o755, exist_ok=True)

    # Permissions on these directories are all 0o777 because of 'mount --bind'
    # limitations but pacman expects them to be 0o755 so we fix them before
    # calling pacstrap (except /var/tmp which is 0o1777).
    fix_permissions_dirs = {
        "boot": 0o755,
        "etc": 0o755,
        "etc/pacman.d": 0o755,
        "var": 0o755,
        "var/lib": 0o755,
        "var/cache": 0o755,
        "var/cache/pacman": 0o755,
        "var/tmp": 0o1777,
        "run": 0o755,
    }

    for dir, permissions in fix_permissions_dirs.items():
        path = root / dir
        if path.exists():
            path.chmod(permissions)

    pacman_conf = workspace(root) / "pacman.conf"
    with pacman_conf.open("w") as f:
        f.write(
            dedent(
                f"""\
                [options]
                RootDir     = {root}
                LogFile     = /dev/null
                CacheDir    = {root}/var/cache/pacman/pkg/
                GPGDir      = {root}/etc/pacman.d/gnupg/
                HookDir     = {root}/etc/pacman.d/hooks/
                HoldPkg     = pacman glibc
                Architecture = auto
                Color
                CheckSpace
                SigLevel    = Required DatabaseOptional TrustAll

                [core]
                {server}

                [extra]
                {server}

                [community]
                {server}
                """
            )
        )

        if args.repositories:
            for repository in args.repositories:
                # repositories must be passed in the form <repo name>::<repo url>
                repository_name, repository_server = repository.split("::", 1)

                # note: for additional repositories, signature checking options are set to pacman's default values
                f.write(
                    dedent(
                        f"""\

                        [{repository_name}]
                        SigLevel = Optional TrustedOnly
                        Server = {repository_server}
                        """
                    )
                )

    if not do_run_build_script and args.bootable:
        hooks_dir = root / "etc/pacman.d/hooks"
        scripts_dir = root / "etc/pacman.d/scripts"

        os.makedirs(hooks_dir, 0o755, exist_ok=True)
        os.makedirs(scripts_dir, 0o755, exist_ok=True)

        # Disable depmod pacman hook as depmod is handled by kernel-install as well.
        hooks_dir.joinpath("60-depmod.hook").symlink_to("/dev/null")

        write_resource(hooks_dir / "90-mkosi-kernel-add.hook", "mkosi.resources.arch", "90_kernel_add.hook")
        write_resource(scripts_dir / "mkosi-kernel-add", "mkosi.resources.arch", "kernel_add.sh",
                       executable=True)

        write_resource(hooks_dir / "60-mkosi-kernel-remove.hook", "mkosi.resources.arch", "60_kernel_remove.hook")
        write_resource(scripts_dir / "mkosi-kernel-remove", "mkosi.resources.arch", "kernel_remove.sh",
                       executable=True)

        if args.esp_partno is not None:
            write_resource(hooks_dir / "91-mkosi-bootctl-update.hook", "mkosi.resources.arch", "91_bootctl_update.hook")

        if args.bios_partno is not None:
            write_resource(hooks_dir / "90-mkosi-vmlinuz-add.hook", "mkosi.resources.arch", "90_vmlinuz_add.hook")
            write_resource(hooks_dir / "60-mkosi-vmlinuz-remove.hook", "mkosi.resources.arch", "60_vmlinuz_remove.hook")

    keyring = "archlinux"
    if platform.machine() == "aarch64":
        keyring += "arm"

    packages: Set[str] = set()
    add_packages(args, packages, "base")

    if not do_run_build_script and args.bootable:
        if args.output_format == OutputFormat.gpt_btrfs:
            add_packages(args, packages, "btrfs-progs")
        elif args.output_format == OutputFormat.gpt_xfs:
            add_packages(args, packages, "xfsprogs")
        if args.encrypt:
            add_packages(args, packages, "cryptsetup", "device-mapper")
        if args.bios_partno:
            add_packages(args, packages, "grub")

        add_packages(args, packages, "dracut", "binutils")
        configure_dracut(args, root)

    packages.update(args.packages)

    official_kernel_packages = {
        "linux",
        "linux-lts",
        "linux-hardened",
        "linux-zen",
    }

    has_kernel_package = official_kernel_packages.intersection(args.packages)
    if not do_run_build_script and args.bootable and not has_kernel_package:
        # No user-specified kernel
        add_packages(args, packages, "linux")

    if do_run_build_script:
        packages.update(args.build_packages)

    if not do_run_build_script and args.ssh:
        add_packages(args, packages, "openssh")

    with mount_api_vfs(args, root):
        run_pacman(root, pacman_conf, packages)

    patch_locale_gen(args, root)
    run_workspace_command(args, root, ["/usr/bin/locale-gen"])

    root.joinpath("etc/locale.conf").write_text("LANG=en_US.UTF-8\n")

    # Arch still uses pam_securetty which prevents root login into
    # systemd-nspawn containers. See https://bugs.archlinux.org/task/45903.
    disable_pam_securetty(root)


@complete_step("Installing openSUSE…")
def install_opensuse(args: CommandLineArguments, root: Path, do_run_build_script: bool) -> None:
    release = args.release.strip('"')

    # If the release looks like a timestamp, it's Tumbleweed. 13.x is legacy (14.x won't ever appear). For
    # anything else, let's default to Leap.
    if release.isdigit() or release == "tumbleweed":
        release_url = f"{args.mirror}/tumbleweed/repo/oss/"
        updates_url = f"{args.mirror}/update/tumbleweed/"
    elif release == "leap":
        release_url = f"{args.mirror}/distribution/leap/15.1/repo/oss/"
        updates_url = f"{args.mirror}/update/leap/15.1/oss/"
    elif release == "current":
        release_url = f"{args.mirror}/distribution/openSUSE-stable/repo/oss/"
        updates_url = f"{args.mirror}/update/openSUSE-current/"
    elif release == "stable":
        release_url = f"{args.mirror}/distribution/openSUSE-stable/repo/oss/"
        updates_url = f"{args.mirror}/update/openSUSE-stable/"
    else:
        release_url = f"{args.mirror}/distribution/leap/{release}/repo/oss/"
        updates_url = f"{args.mirror}/update/leap/{release}/oss/"

    # Configure the repositories: we need to enable packages caching here to make sure that the package cache
    # stays populated after "zypper install".
    run(["zypper", "--root", root, "addrepo", "-ck", release_url, "repo-oss"])
    run(["zypper", "--root", root, "addrepo", "-ck", updates_url, "repo-update"])

    if not args.with_docs:
        root.joinpath("etc/zypp/zypp.conf").write_text("rpm.install.excludedocs = yes\n")

    packages = {*args.packages}
    add_packages(args, packages, "systemd")

    if release.startswith("42."):
        add_packages(args, packages, "patterns-openSUSE-minimal_base")
    else:
        add_packages(args, packages, "patterns-base-minimal_base")

    if not do_run_build_script and args.bootable:
        add_packages(args, packages, "kernel-default", "dracut", "binutils")
        configure_dracut(args, root)

        if args.bios_partno is not None:
            add_packages(args, packages, "grub2")

    if not do_run_build_script and args.encrypt:
        add_packages(args, packages, "device-mapper")

    if args.output_format in (OutputFormat.subvolume, OutputFormat.gpt_btrfs):
        add_packages(args, packages, "btrfsprogs")

    if do_run_build_script:
        packages.update(args.build_packages)

    if not do_run_build_script and args.ssh:
        add_packages(args, packages, "openssh-server")

    cmdline: List[PathString] = [
        "zypper",
        "--root",
        root,
        "--gpg-auto-import-keys",
        "install",
        "-y",
        "--no-recommends",
        "--download-in-advance",
        *sort_packages(packages),
    ]

    with mount_api_vfs(args, root):
        run(cmdline)

    # Disable package caching in the image that was enabled previously to populate the package cache.
    run(["zypper", "--root", root, "modifyrepo", "-K", "repo-oss"])
    run(["zypper", "--root", root, "modifyrepo", "-K", "repo-update"])

    if args.password == "":
        shutil.copy2(root / "usr/etc/pam.d/common-auth", root / "etc/pam.d/common-auth")

        def jj(line: str) -> str:
            if "pam_unix.so" in line:
                return f"{line.strip()} nullok"
            return line

        patch_file(root / "etc/pam.d/common-auth", jj)

    if args.autologin:
        # copy now, patch later (in set_autologin())
        shutil.copy2(root / "usr/etc/pam.d/login", root / "etc/pam.d/login")


def install_distribution(args: CommandLineArguments, root: Path, do_run_build_script: bool, cached: bool) -> None:
    if cached:
        return

    install: Dict[Distribution, Callable[[CommandLineArguments, Path, bool], None]] = {
        Distribution.fedora: install_fedora,
        Distribution.centos: install_centos,
        Distribution.centos_epel: install_centos,
        Distribution.mageia: install_mageia,
        Distribution.debian: install_debian,
        Distribution.ubuntu: install_ubuntu,
        Distribution.arch: install_arch,
        Distribution.opensuse: install_opensuse,
        Distribution.clear: install_clear,
        Distribution.photon: install_photon,
        Distribution.openmandriva: install_openmandriva,
        Distribution.rocky: install_rocky,
        Distribution.rocky_epel: install_rocky,
        Distribution.alma: install_alma,
        Distribution.alma_epel: install_alma,
    }

    disable_kernel_install(args, root)

    with mount_cache(args, root):
        install[args.distribution](args, root, do_run_build_script)

    reenable_kernel_install(args, root)


def reset_machine_id(args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool) -> None:
    """Make /etc/machine-id an empty file.

    This way, on the next boot is either initialized and committed (if /etc is
    writable) or the image runs with a transient machine ID, that changes on
    each boot (if the image is read-only).
    """

    if do_run_build_script:
        return
    if for_cache:
        return

    with complete_step("Resetting machine ID"):
        machine_id = root / "etc/machine-id"
        try:
            machine_id.unlink()
        except FileNotFoundError:
            pass
        machine_id.touch()

        dbus_machine_id = root / "var/lib/dbus/machine-id"
        try:
            dbus_machine_id.unlink()
        except FileNotFoundError:
            pass
        else:
            dbus_machine_id.symlink_to("../../../etc/machine-id")


def reset_random_seed(args: CommandLineArguments, root: Path) -> None:
    """Remove random seed file, so that it is initialized on first boot"""
    random_seed = root / "var/lib/systemd/random-seed"
    if not random_seed.exists():
        return

    with complete_step("Removing random seed"):
        random_seed.unlink()


def set_root_password(args: CommandLineArguments, root: Path, do_run_build_script: bool, cached: bool) -> None:
    "Set the root account password, or just delete it so it's easy to log in"

    if do_run_build_script:
        return
    if cached:
        return

    if args.password == "":
        with complete_step("Deleting root password"):

            def delete_root_pw(line: str) -> str:
                if line.startswith("root:"):
                    return ":".join(["root", ""] + line.split(":")[2:])
                return line

            patch_file(root / "etc/passwd", delete_root_pw)
    elif args.password:
        with complete_step("Setting root password"):
            if args.password_is_hashed:
                password = args.password
            else:
                password = crypt.crypt(args.password, crypt.mksalt(crypt.METHOD_SHA512))

            def set_root_pw(line: str) -> str:
                if line.startswith("root:"):
                    return ":".join(["root", password] + line.split(":")[2:])
                return line

            patch_file(root / "etc/shadow", set_root_pw)


def invoke_fstrim(args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool) -> None:

    if do_run_build_script:
        return
    if is_generated_root(args):
        return
    if not args.output_format.is_disk():
        return
    if for_cache:
        return

    with complete_step("Trimming File System"):
        run(["fstrim", "-v", root], check=False)


def pam_add_autologin(root: Path, tty: str) -> None:
    with open(root / "etc/pam.d/login", "r+") as f:
        original = f.read()
        f.seek(0)
        f.write(f"auth sufficient pam_succeed_if.so tty = {tty}\n")
        f.write(original)


def set_autologin(args: CommandLineArguments, root: Path, do_run_build_script: bool, cached: bool) -> None:
    if do_run_build_script or cached or not args.autologin:
        return

    with complete_step("Setting up autologin…"):
        # On Arch, Debian, PAM wants the full path to the console device or it will refuse access
        device_prefix = "/dev/" if args.distribution in [Distribution.arch, Distribution.debian] else ""

        override_dir = root / "etc/systemd/system/console-getty.service.d"
        override_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        write_resource(override_dir / "autologin.conf", "mkosi.resources", "console_getty_autologin.conf",
                       mode=0o644)

        pam_add_autologin(root, f"{device_prefix}pts/0")

        override_dir = root / "etc/systemd/system/serial-getty@ttyS0.service.d"
        override_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        write_resource(override_dir / "autologin.conf", "mkosi.resources", "serial_getty_autologin.conf",
                       mode=0o644)

        pam_add_autologin(root, f"{device_prefix}ttyS0")

        override_dir = root / "etc/systemd/system/getty@tty1.service.d"
        override_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        write_resource(override_dir / "autologin.conf", "mkosi.resources", "getty_autologin.conf",
                       mode=0o644)

        pam_add_autologin(root, f"{device_prefix}tty1")
        pam_add_autologin(root, f"{device_prefix}console")


def set_serial_terminal(args: CommandLineArguments, root: Path, do_run_build_script: bool, cached: bool) -> None:
    """Override TERM for the serial console with the terminal type from the host."""

    if do_run_build_script or cached or not args.qemu_headless:
        return

    with complete_step("Configuring serial tty (/dev/ttyS0)…"):
        override_dir = root / "etc/systemd/system/serial-getty@ttyS0.service.d"
        os.makedirs(override_dir, mode=0o755, exist_ok=True)

        columns, lines = shutil.get_terminal_size(fallback=(80, 24))
        override_file = override_dir / "term.conf"
        override_file.write_text(
            dedent(
                f"""\
                [Service]
                Environment=TERM={os.getenv('TERM', 'vt220')}
                Environment=COLUMNS={columns}
                Environment=LINES={lines}
                """
            )
        )

        override_file.chmod(0o644)


def nspawn_params_for_build_sources(args: CommandLineArguments, sft: SourceFileTransfer) -> List[str]:
    params = []

    if args.build_sources is not None:
        params += ["--setenv=SRCDIR=/root/src",
                   "--chdir=/root/src"]
        if sft == SourceFileTransfer.mount:
            params += [f"--bind={args.build_sources}:/root/src"]

        if args.read_only:
            params += ["--overlay=+/root/src::/root/src"]
    else:
        params += ["--chdir=/root"]

    params += [f"--setenv={env}" for env in args.environment]

    return params


def run_prepare_script(args: CommandLineArguments, root: Path, do_run_build_script: bool, cached: bool) -> None:
    if args.prepare_script is None:
        return
    if cached:
        return

    verb = "build" if do_run_build_script else "final"

    with mount_cache(args, root), complete_step("Running prepare script…"):

        # We copy the prepare script into the build tree. We'd prefer
        # mounting it into the tree, but for that we'd need a good
        # place to mount it to. But if we create that we might as well
        # just copy the file anyway.

        shutil.copy2(args.prepare_script, root_home(args, root) / "prepare")

        nspawn_params = nspawn_params_for_build_sources(args, SourceFileTransfer.mount)
        run_workspace_command(args, root, ["/root/prepare", verb], network=True, nspawn_params=nspawn_params)

        srcdir = root_home(args, root) / "src"
        if srcdir.exists():
            os.rmdir(srcdir)

        os.unlink(root_home(args, root) / "prepare")


def run_postinst_script(
    args: CommandLineArguments, root: Path, loopdev: Optional[Path], do_run_build_script: bool, for_cache: bool
) -> None:
    if args.postinst_script is None:
        return
    if for_cache:
        return

    verb = "build" if do_run_build_script else "final"

    with mount_cache(args, root), complete_step("Running postinstall script…"):

        # We copy the postinst script into the build tree. We'd prefer
        # mounting it into the tree, but for that we'd need a good
        # place to mount it to. But if we create that we might as well
        # just copy the file anyway.

        shutil.copy2(args.postinst_script, root_home(args, root) / "postinst")

        nspawn_params = []
        # in order to have full blockdev access, i.e. for making grub2 bootloader changes
        # we need to have these bind mounts for a proper chroot setup
        if args.bootable:
            if loopdev is None:
                raise ValueError("Parameter 'loopdev' required for bootable images.")
            nspawn_params += nspawn_params_for_blockdev_access(args, loopdev)

        run_workspace_command(
            args, root, ["/root/postinst", verb], network=(args.with_network is True), nspawn_params=nspawn_params
        )
        root_home(args, root).joinpath("postinst").unlink()


def output_dir(args: CommandLineArguments) -> Path:
    return args.output_dir or Path(os.getcwd())


def run_finalize_script(args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool) -> None:
    if args.finalize_script is None:
        return
    if for_cache:
        return

    verb = "build" if do_run_build_script else "final"

    with complete_step("Running finalize script…"):
        env = dict(cast(Tuple[str, str], v.split("=", maxsplit=1)) for v in args.environment)
        env = collections.ChainMap(dict(BUILDROOT=root, OUTPUTDIR=output_dir(args)), env, os.environ)
        run([args.finalize_script, verb], env=env)


def install_boot_loader_clear(args: CommandLineArguments, root: Path, loopdev: Path) -> None:
    # clr-boot-manager uses blkid in the device backing "/" to
    # figure out uuid and related parameters.
    nspawn_params = nspawn_params_for_blockdev_access(args, loopdev)

    cmdline = ["/usr/bin/clr-boot-manager", "update", "-i"]
    run_workspace_command(args, root, cmdline, nspawn_params=nspawn_params)


def install_boot_loader_centos_old_efi(args: CommandLineArguments, root: Path, loopdev: Path) -> None:
    nspawn_params = nspawn_params_for_blockdev_access(args, loopdev)

    # prepare EFI directory on ESP
    os.makedirs(root / "efi/EFI/centos", exist_ok=True)

    # patch existing or create minimal GRUB_CMDLINE config
    write_grub_config(args, root)

    # generate grub2 efi boot config
    cmdline = ["/sbin/grub2-mkconfig", "-o", "/efi/EFI/centos/grub.cfg"]
    run_workspace_command(args, root, cmdline, nspawn_params=nspawn_params)

    # if /sys/firmware/efi is not present within systemd-nspawn the grub2-mkconfig makes false assumptions, let's fix this
    def _fix_grub(line: str) -> str:
        if "linux16" in line:
            return line.replace("linux16", "linuxefi")
        elif "initrd16" in line:
            return line.replace("initrd16", "initrdefi")
        return line

    patch_file(root / "efi/EFI/centos/grub.cfg", _fix_grub)


def install_boot_loader(
    args: CommandLineArguments, root: Path, loopdev: Optional[Path], do_run_build_script: bool, cached: bool
) -> None:
    if not args.bootable or do_run_build_script:
        return
    assert loopdev is not None

    if cached:
        return

    with complete_step("Installing boot loader…"):
        if args.esp_partno:
            if args.distribution == Distribution.clear:
                pass
            elif args.distribution in (Distribution.centos, Distribution.centos_epel) and is_older_than_centos8(
                args.release
            ):
                install_boot_loader_centos_old_efi(args, root, loopdev)
            else:
                run_workspace_command(args, root, ["bootctl", "install"])

        if args.bios_partno and args.distribution != Distribution.clear:
            grub = (
                "grub"
                if args.distribution in (Distribution.ubuntu, Distribution.debian, Distribution.arch)
                else "grub2"
            )
            # TODO: Just use "grub" once https://github.com/systemd/systemd/pull/16645 is widely available.
            if args.distribution in (Distribution.ubuntu, Distribution.debian, Distribution.opensuse):
                grub = f"/usr/sbin/{grub}"

            install_grub(args, root, loopdev, grub)

        if args.distribution == Distribution.clear:
            install_boot_loader_clear(args, root, loopdev)


def install_extra_trees(args: CommandLineArguments, root: Path, for_cache: bool) -> None:
    if not args.extra_trees:
        return

    if for_cache:
        return

    with complete_step("Copying in extra file trees…"):
        for tree in args.extra_trees:
            if tree.is_dir():
                copy_path(tree, root)
            else:
                # unpack_archive() groks Paths, but mypy doesn't know this.
                # Pretend that tree is a str.
                shutil.unpack_archive(cast(str, tree), root)


def install_skeleton_trees(args: CommandLineArguments, root: Path, cached: bool) -> None:
    if not args.skeleton_trees:
        return

    if cached:
        return

    with complete_step("Copying in skeleton file trees…"):
        for tree in args.skeleton_trees:
            if tree.is_dir():
                copy_path(tree, root)
            else:
                # unpack_archive() groks Paths, but mypy doesn't know this.
                # Pretend that tree is a str.
                shutil.unpack_archive(cast(str, tree), root)


def copy_git_files(src: Path, dest: Path, *, source_file_transfer: SourceFileTransfer) -> None:
    what_files = ["--exclude-standard", "--cached"]
    if source_file_transfer == SourceFileTransfer.copy_git_others:
        what_files += ["--others", "--exclude=.mkosi-*"]

    c = run(["git", "-C", src, "ls-files", "-z", *what_files], stdout=PIPE, universal_newlines=False, check=True)
    files = {x.decode("utf-8") for x in c.stdout.rstrip(b"\0").split(b"\0")}

    # Add the .git/ directory in as well.
    if source_file_transfer == SourceFileTransfer.copy_git_more:
        top = os.path.join(src, ".git/")
        for path, _, filenames in os.walk(top):
            for filename in filenames:
                fp = os.path.join(path, filename)  # full path
                fr = os.path.join(".git/", fp[len(top) :])  # relative to top
                files.add(fr)

    # Get submodule files
    c = run(["git", "-C", src, "submodule", "status", "--recursive"], stdout=PIPE, universal_newlines=True, check=True)
    submodules = {x.split()[1] for x in c.stdout.splitlines()}

    # workaround for git-ls-files returning the path of submodules that we will
    # still parse
    files -= submodules

    for sm in submodules:
        c = run(
            ["git", "-C", os.path.join(src, sm), "ls-files", "-z"] + what_files,
            stdout=PIPE,
            universal_newlines=False,
            check=True,
        )
        files |= {os.path.join(sm, x.decode("utf-8")) for x in c.stdout.rstrip(b"\0").split(b"\0")}
        files -= submodules

    del c

    for path in files:
        src_path = os.path.join(src, path)
        dest_path = os.path.join(dest, path)

        directory = os.path.dirname(dest_path)
        os.makedirs(directory, exist_ok=True)

        copy_file(src_path, dest_path)


def install_build_src(args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool) -> None:
    if for_cache:
        return

    if args.build_script is None:
        return

    if do_run_build_script:
        with complete_step("Copying in build script…"):
            copy_file(args.build_script, root_home(args, root) / args.build_script.name)

    sft: Optional[SourceFileTransfer] = None
    resolve_symlinks: bool = False
    if do_run_build_script:
        sft = args.source_file_transfer
        resolve_symlinks = args.source_resolve_symlinks
    else:
        sft = args.source_file_transfer_final
        resolve_symlinks = args.source_resolve_symlinks_final

    if args.build_sources is None or sft is None:
        return

    with complete_step("Copying in sources…"):
        target = root_home(args, root) / "src"

        if sft in (
            SourceFileTransfer.copy_git_others,
            SourceFileTransfer.copy_git_cached,
            SourceFileTransfer.copy_git_more,
        ):
            copy_git_files(args.build_sources, target, source_file_transfer=sft)
        elif sft == SourceFileTransfer.copy_all:
            ignore = shutil.ignore_patterns(
                ".git",
                ".mkosi-*",
                "*.cache-pre-dev",
                "*.cache-pre-inst",
                f"{args.output_dir.name}/" if args.output_dir else "mkosi.output/",
                f"{args.workspace_dir.name}/" if args.workspace_dir else "mkosi.workspace/",
                f"{args.cache_path.name}/" if args.cache_path else "mkosi.cache/",
                f"{args.build_dir.name}/" if args.build_dir else "mkosi.builddir/",
                f"{args.include_dir.name}/" if args.include_dir else "mkosi.includedir/",
                f"{args.install_dir.name}/" if args.install_dir else "mkosi.installdir/",
            )
            shutil.copytree(args.build_sources, target, symlinks=not resolve_symlinks, ignore=ignore)


def install_build_dest(args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool) -> None:
    if do_run_build_script:
        return
    if for_cache:
        return

    if args.build_script is None:
        return

    with complete_step("Copying in build tree…"):
        copy_path(install_dir(args, root), root)


def make_read_only(args: CommandLineArguments, root: Path, for_cache: bool, b: bool = True) -> None:
    if not args.read_only:
        return
    if for_cache:
        return

    if args.output_format not in (OutputFormat.gpt_btrfs, OutputFormat.subvolume):
        return
    if is_generated_root(args):
        return

    with complete_step("Marking root subvolume read-only"):
        btrfs_subvol_make_ro(root, b)


def xz_binary() -> str:
    return "pxz" if shutil.which("pxz") else "xz"


def compressor_command(option: Union[str, bool]) -> List[str]:
    """Returns a command suitable for compressing archives."""

    if option == "xz":
        return [xz_binary(), "--check=crc32", "--lzma2=dict=1MiB", "-T0"]
    elif option == "zstd":
        return ["zstd", "-15", "-q", "-T0"]
    elif option is False:
        return ["cat"]
    else:
        die(f"Unknown compression {option}")


def tar_binary() -> str:
    # Some distros (Mandriva) install BSD tar as "tar", hence prefer
    # "gtar" if it exists, which should be GNU tar wherever it exists.
    # We are interested in exposing same behaviour everywhere hence
    # it's preferable to use the same implementation of tar
    # everywhere. In particular given the limited/different SELinux
    # support in BSD tar and the different command line syntax
    # compared to GNU tar.
    return "gtar" if shutil.which("gtar") else "tar"


def make_tar(args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool) -> Optional[BinaryIO]:
    if do_run_build_script:
        return None
    if args.output_format != OutputFormat.tar:
        return None
    if for_cache:
        return None

    root_dir = root / "usr" if args.usr_only else root

    cmd: List[PathString] = [tar_binary(), "-C", root_dir, "-c", "--xattrs", "--xattrs-include=*"]
    if args.tar_strip_selinux_context:
        cmd += ["--xattrs-exclude=security.selinux"]

    compress = should_compress_output(args)
    if compress:
        cmd += ["--use-compress-program=" + " ".join(compressor_command(compress))]

    cmd += ["."]

    with complete_step("Creating archive…"):
        f: BinaryIO = cast(BinaryIO, tempfile.NamedTemporaryFile(dir=os.path.dirname(args.output), prefix=".mkosi-"))
        run(cmd, stdout=f)

    return f


def find_files(root: Path) -> Generator[Path, None, None]:
    """Generate a list of all filepaths relative to @root"""
    queue: Deque[Union[str, Path]] = collections.deque([root])

    while queue:
        for entry in os.scandir(queue.pop()):
            yield Path(entry.path).relative_to(root)
            if entry.is_dir(follow_symlinks=False):
                queue.append(entry.path)


def make_cpio(
    args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool
) -> Optional[BinaryIO]:
    if do_run_build_script:
        return None
    if args.output_format != OutputFormat.cpio:
        return None
    if for_cache:
        return None

    root_dir = root / "usr" if args.usr_only else root

    with complete_step("Creating archive…"):
        f: BinaryIO = cast(BinaryIO, tempfile.NamedTemporaryFile(dir=os.path.dirname(args.output), prefix=".mkosi-"))

        compressor = compressor_command(should_compress_output(args))
        files = find_files(root_dir)
        cmd: List[PathString] = [
            "cpio", "-o", "--reproducible", "--null", "-H", "newc", "--quiet", "-D", root_dir
        ]

        with spawn(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as cpio:
            #  https://github.com/python/mypy/issues/10583
            assert cpio.stdin is not None

            with spawn(compressor, stdin=cpio.stdout, stdout=f, delay_interrupt=False):
                for file in files:
                    cpio.stdin.write(os.fspath(file).encode("utf8") + b"\0")
                cpio.stdin.close()
        if cpio.wait() != 0:
            die("Failed to create archive")

    return f


def generate_squashfs(args: CommandLineArguments, root: Path, for_cache: bool) -> Optional[BinaryIO]:
    if not args.output_format.is_squashfs():
        return None
    if for_cache:
        return None

    command = args.mksquashfs_tool[0] if args.mksquashfs_tool else "mksquashfs"
    comp_args = args.mksquashfs_tool[1:] if args.mksquashfs_tool and args.mksquashfs_tool[1:] else ["-noappend"]

    compress = should_compress_fs(args)
    # mksquashfs default is true, so no need to specify anything to have the default compression.
    if isinstance(compress, str):
        comp_args += ["-comp", compress]
    elif compress is False:
        comp_args += ["-noI", "-noD", "-noF", "-noX"]

    with complete_step("Creating squashfs file system…"):
        f: BinaryIO = cast(
            BinaryIO, tempfile.NamedTemporaryFile(prefix=".mkosi-squashfs", dir=os.path.dirname(args.output))
        )
        run([command, root, f.name, *comp_args])

    return f


def generate_ext4(args: CommandLineArguments, root: Path, label: str, for_cache: bool) -> Optional[BinaryIO]:
    if args.output_format != OutputFormat.gpt_ext4:
        return None
    if for_cache:
        return None

    with complete_step("Creating ext4 root file system…"):
        f: BinaryIO = cast(
            BinaryIO, tempfile.NamedTemporaryFile(prefix=".mkosi-mkfs-ext4", dir=os.path.dirname(args.output))
        )
        f.truncate(args.root_size)
        run(["mkfs.ext4", "-I", "256", "-L", label, "-M", "/", "-d", root, f.name])

    if args.minimize:
        with complete_step("Minimizing ext4 root file system…"):
            run(["resize2fs", "-M", f.name])

    return f


def generate_btrfs(args: CommandLineArguments, root: Path, label: str, for_cache: bool) -> Optional[BinaryIO]:
    if args.output_format != OutputFormat.gpt_btrfs:
        return None
    if for_cache:
        return None

    with complete_step("Creating minimal btrfs root file system…"):
        f: BinaryIO = cast(BinaryIO, tempfile.NamedTemporaryFile(prefix=".mkosi-mkfs-btrfs", dir=args.output.parent))
        f.truncate(args.root_size)

        cmdline: Sequence[PathString] = [
            "mkfs.btrfs", "-L", label, "-d", "single", "-m", "single", "--rootdir", root, f.name
        ]

        if args.minimize:
            try:
                run([*cmdline, "--shrink"])
            except subprocess.CalledProcessError:
                # The --shrink option was added in btrfs-tools 4.14.1, before that it was the default behaviour.
                # If the above fails, let's see if things work if we drop it
                run(cmdline)
        else:
            run(cmdline)

    return f


def make_generated_root(args: CommandLineArguments, root: Path, for_cache: bool) -> Optional[BinaryIO]:

    if not is_generated_root(args):
        return None

    label = "usr" if args.usr_only else "root"
    patched_root = root / "usr" if args.usr_only else root

    if args.output_format == OutputFormat.gpt_ext4:
        return generate_ext4(args, patched_root, label, for_cache)
    if args.output_format == OutputFormat.gpt_btrfs:
        return generate_btrfs(args, patched_root, label, for_cache)
    if args.output_format.is_squashfs():
        return generate_squashfs(args, patched_root, for_cache)

    return None


@dataclasses.dataclass
class PartitionTable:
    partitions: List[str]
    last_partition_sector: Optional[int]
    sector_size: int
    first_lba: Optional[int]

    grain: int = 4096

    @classmethod
    def read(cls, loopdev: Path) -> PartitionTable:
        table = []
        last_sector = 0
        sector_size = 512
        first_lba = None

        c = run(["sfdisk", "--dump", loopdev],
                stdout=PIPE,
                universal_newlines=True)

        if 'disk' in ARG_DEBUG:
            print_between_lines(c.stdout)

        in_body = False
        for line in c.stdout.splitlines():
            line = line.strip()

            if line.startswith('sector-size:'):
                sector_size = int(line[12:])
            if line.startswith('first-lba:'):
                first_lba = int(line[10:])

            if line == "":  # empty line is where the body begins
                in_body = True
                continue
            if not in_body:
                continue

            table += [line]

            _, rest = line.split(":", 1)
            fields = rest.split(",")

            start = None
            size = None

            for field in fields:
                field = field.strip()

                if field.startswith("start="):
                    start = int(field[6:])
                if field.startswith("size="):
                    size = int(field[5:])

            if start is not None and size is not None:
                end = start + size
                last_sector = max(last_sector, end)

        return cls(table, last_sector * sector_size, sector_size, first_lba)

    @classmethod
    def empty(cls, first_lba: Optional[int] = None) -> PartitionTable:
        return cls([], None, 512, first_lba)

    def first_usable_offset(self, max_partitions: int = 128) -> int:
        if self.last_partition_sector:
            return roundup(self.last_partition_sector, self.grain)
        elif self.first_lba is not None:
            # No rounding here, we honour the specified value exactly.
            return self.first_lba * self.sector_size
        else:
            # The header is like the footer, but we have a one-sector "protective MBR" at offset 0
            return roundup(self.sector_size + self.footer_size(), self.grain)

    def footer_size(self, max_partitions: int = 128) -> int:
        # The footer must have enough space for the GPT header (one sector),
        # and the GPT parition entry area. PEA size of 16384 (128 partitions)
        # is recommended.
        pea_sectors = math.ceil(max_partitions * 128 / self.sector_size)
        return (1 + pea_sectors) * self.sector_size


def insert_partition(
    args: CommandLineArguments,
    raw: BinaryIO,
    loopdev: Path,
    partno: int,
    blob: BinaryIO,
    name: str,
    type_uuid: uuid.UUID,
    read_only: bool,
    uuid_opt: Optional[uuid.UUID] = None,
) -> int:
    if args.ran_sfdisk:
        old_table = PartitionTable.read(loopdev)
    else:
        # No partition table yet? Then let's fake one...
        old_table = PartitionTable.empty(args.gpt_first_lba)

    blob_size = roundup(os.stat(blob.name).st_size, 512)
    luks_extra = 16 * 1024 * 1024 if args.encrypt == "all" else 0
    partition_offset = old_table.first_usable_offset()
    new_size = roundup(partition_offset + blob_size + luks_extra + old_table.footer_size(), 4096)

    ss = f" ({new_size // old_table.sector_size} sectors)" if 'disk' in ARG_DEBUG else ""
    MkosiPrinter.print_step(f"Resizing disk image to {format_bytes(new_size)}{ss}")

    os.truncate(raw.name, new_size)
    run(["losetup", "--set-capacity", loopdev])

    ss = f" ({blob_size // old_table.sector_size} sectors)" if 'disk' in ARG_DEBUG else ""
    MkosiPrinter.print_step(f"Inserting partition of {format_bytes(blob_size)}{ss}...")

    if args.gpt_first_lba is not None:
        first_lba: Optional[int] = args.gpt_first_lba
    elif old_table.partitions:
        first_lba = None   # no need to specify this if we already have partitions
    else:
        first_lba = partition_offset // old_table.sector_size

    new = []
    if uuid_opt is not None:
        new += [f'uuid={uuid_opt}']

    n_sectors = (blob_size + luks_extra) // 512
    new += [f'size={n_sectors}',
            f'type={type_uuid}',
            f'attrs={"GUID:60" if read_only else ""}',
            f'name="{name}"']

    table = ["label: gpt",
             f"grain: {old_table.grain}"]
    if first_lba is not None:
        table += [f"first-lba: {first_lba}"]

    table += [*old_table.partitions,
              ', '.join(new)]

    if 'disk' in ARG_DEBUG:
        print_between_lines('\n'.join(table))

    run(["sfdisk", "--color=never", "--no-reread", "--no-tell-kernel", loopdev],
        input='\n'.join(table).encode("utf-8"))
    run(["sync"])
    run_with_backoff(["blockdev", "--rereadpt", loopdev], attempts=10)

    MkosiPrinter.print_step("Writing partition...")

    if args.root_partno == partno:
        luks_format_root(args, loopdev, False, False, True)
        cm = luks_setup_root(args, loopdev, False, True)
    else:
        cm = contextlib.nullcontext()

    with cm as dev:
        path = dev if dev is not None else partition(loopdev, partno)
        run(["dd", f"if={blob.name}", f"of={path}", "conv=nocreat,sparse"])

    args.ran_sfdisk = True

    return blob_size


def insert_generated_root(
    args: CommandLineArguments,
    raw: Optional[BinaryIO],
    loopdev: Optional[Path],
    image: Optional[BinaryIO],
    for_cache: bool,
) -> None:
    if not is_generated_root(args):
        return
    if not args.output_format.is_disk():
        return
    if for_cache:
        return
    assert raw is not None
    assert loopdev is not None
    assert image is not None
    assert args.root_partno is not None

    with complete_step("Inserting generated root partition…"):
        args.root_size = insert_partition(
            args,
            raw,
            loopdev,
            args.root_partno,
            image,
            root_partition_name(args),
            gpt_root_native(args.architecture, args.usr_only).root,
            args.read_only,
        )


def make_verity(
    args: CommandLineArguments, dev: Optional[Path], do_run_build_script: bool, for_cache: bool
) -> Tuple[Optional[BinaryIO], Optional[str]]:
    if do_run_build_script or not args.verity:
        return None, None
    if for_cache:
        return None, None
    assert dev is not None

    with complete_step("Generating verity hashes…"):
        f: BinaryIO = cast(BinaryIO, tempfile.NamedTemporaryFile(dir=args.output.parent, prefix=".mkosi-"))
        c = run(["veritysetup", "format", dev, f.name], stdout=PIPE)

        for line in c.stdout.decode("utf-8").split("\n"):
            if line.startswith("Root hash:"):
                root_hash = line[10:].strip()
                return f, root_hash

        raise ValueError("Root hash not found")


def insert_verity(
    args: CommandLineArguments,
    raw: Optional[BinaryIO],
    loopdev: Optional[Path],
    verity: Optional[BinaryIO],
    root_hash: Optional[str],
    for_cache: bool,
) -> None:
    if verity is None:
        return
    if for_cache:
        return
    assert loopdev is not None
    assert raw is not None
    assert root_hash is not None
    assert args.verity_partno is not None

    # Use the final 128 bit of the root hash as partition UUID of the verity partition
    u = uuid.UUID(root_hash[-32:])

    with complete_step("Inserting verity partition…"):
        insert_partition(
            args,
            raw,
            loopdev,
            args.verity_partno,
            verity,
            root_partition_name(args, True),
            gpt_root_native(args.architecture, args.usr_only).verity,
            True,
            u,
        )


def patch_root_uuid(
    args: CommandLineArguments, loopdev: Optional[Path], root_hash: Optional[str], for_cache: bool
) -> None:
    if root_hash is None:
        return
    assert loopdev is not None

    if for_cache:
        return

    # Use the first 128bit of the root hash as partition UUID of the root partition
    u = uuid.UUID(root_hash[:32])

    with complete_step("Patching root partition UUID…"):
        run(["sfdisk", "--part-uuid", loopdev, str(args.root_partno), str(u)], check=True)


def extract_partition(
    args: CommandLineArguments, dev: Optional[Path], do_run_build_script: bool, for_cache: bool
) -> Optional[BinaryIO]:

    if do_run_build_script or for_cache or not args.split_artifacts:
        return None

    assert dev is not None

    with complete_step("Extracting partition…"):
        f: BinaryIO = cast(BinaryIO, tempfile.NamedTemporaryFile(dir=os.path.dirname(args.output), prefix=".mkosi-"))
        run(["dd", f"if={dev}", f"of={f.name}", "conv=nocreat,sparse"])

    return f


def install_unified_kernel(
    args: CommandLineArguments,
    root: Path,
    root_hash: Optional[str],
    do_run_build_script: bool,
    for_cache: bool,
    cached: bool,
    mount: Callable[[], ContextManager[None]],
) -> None:
    # Iterates through all kernel versions included in the image and generates a combined
    # kernel+initrd+cmdline+osrelease EFI file from it and places it in the /EFI/Linux directory of the ESP.
    # sd-boot iterates through them and shows them in the menu. These "unified" single-file images have the
    # benefit that they can be signed like normal EFI binaries, and can encode everything necessary to boot a
    # specific root device, including the root hash.

    if not args.bootable or args.esp_partno is None or not args.with_unified_kernel_images:
        return

    # Don't run dracut if this is for the cache. The unified kernel
    # typically includes the image ID, roothash and other data that
    # differs between cached version and final result. Moreover, we
    # want that the initrd for the image actually takes the changes we
    # make to the image into account (e.g. when we build a systemd
    # test image with this we want that the systemd we just built is
    # in the initrd, and not one from the cache. Hence even though
    # dracut is slow we invoke it only during the last final build,
    # never for the cached builds.
    if for_cache:
        return

    # Don't bother running dracut if this is a development build. Strictly speaking it would probably be a
    # good idea to run it, so that the development environment differs as little as possible from the final
    # build, but then again the initrd should not be relevant for building, and dracut is simply very slow,
    # hence let's avoid it invoking it needlessly, given that we never actually invoke the boot loader on the
    # development image.
    if do_run_build_script:
        return

    with mount(), complete_step("Generating combined kernel + initrd boot file…"):
        # Apparently openmandriva hasn't yet completed its usrmerge so we use lib here instead of usr/lib.
        with os.scandir(root / "lib/modules") as d:
            for kver in d:
                if not (kver.is_dir() and os.path.isfile(os.path.join(kver, "modules.dep"))):
                    continue

                prefix = "/boot" if args.xbootldr_partno is not None else "/efi"
                # While the kernel version can generally be found as a directory under /usr/lib/modules, the
                # kernel image files can be found either in /usr/lib/modules/<kernel-version>/vmlinuz or in
                # /boot depending on the distro. By invoking the kernel-install script directly, we can pass
                # the empty string as the kernel image which causes the script to not pass the --kernel-image
                # option to dracut so it searches the image for us.
                cmdline = [
                    "/etc/kernel/install.d/50-mkosi-dracut-unified-kernel.install",
                    "add",
                    kver.name,
                    f"{prefix}/{args.machine_id}/{kver.name}",
                    "",
                ]

                # Pass some extra meta-info to the script via
                # environment variables. The script uses this to name
                # the unified kernel image file
                env = {}
                if args.image_id is not None:
                    env["IMAGE_ID"] = args.image_id
                if args.image_version is not None:
                    env["IMAGE_VERSION"] = args.image_version
                if root_hash is not None:
                    env["USRHASH" if args.usr_only else "ROOTHASH"] = root_hash

                run_workspace_command(args, root, cmdline, env=env)


def secure_boot_sign(
    args: CommandLineArguments,
    root: Path,
    do_run_build_script: bool,
    for_cache: bool,
    cached: bool,
    mount: Callable[[], ContextManager[None]],
) -> None:
    if do_run_build_script:
        return
    if not args.bootable:
        return
    if not args.secure_boot:
        return
    if for_cache and args.verity:
        return
    if cached and not args.verity:
        return

    with mount():
        for path, _, filenames in os.walk(root / "efi"):
            for i in filenames:
                if not i.endswith(".efi") and not i.endswith(".EFI"):
                    continue

                with complete_step(f"Signing EFI binary {i} in ESP…"):
                    p = os.path.join(path, i)

                    run(
                        [
                            "sbsign",
                            "--key",
                            args.secure_boot_key,
                            "--cert",
                            args.secure_boot_certificate,
                            "--output",
                            p + ".signed",
                            p,
                        ],
                        check=True,
                    )

                    os.rename(p + ".signed", p)


def extract_unified_kernel(
    args: CommandLineArguments,
    root: Path,
    do_run_build_script: bool,
    for_cache: bool,
    mount: Callable[[], ContextManager[None]],
) -> Optional[BinaryIO]:

    if do_run_build_script or for_cache or not args.split_artifacts or not args.bootable:
        return None

    with mount():
        kernel = None

        for path, _, filenames in os.walk(root / "efi/EFI/Linux"):
            for i in filenames:
                if not i.endswith(".efi") and not i.endswith(".EFI"):
                    continue

                if kernel is not None:
                    raise ValueError(
                        f"Multiple kernels found, don't know which one to extract. ({kernel} vs. {path}/{i})"
                    )

                kernel = os.path.join(path, i)

        if kernel is None:
            raise ValueError("No kernel found in image, can't extract")

        assert args.output_split_kernel is not None

        f = copy_file_temporary(kernel, args.output_split_kernel.parent)

    return f


def compress_output(
    args: CommandLineArguments, data: Optional[BinaryIO], suffix: Optional[str] = None
) -> Optional[BinaryIO]:
    if data is None:
        return None
    compress = should_compress_output(args)

    if not compress:
        return data

    with complete_step(f"Compressing output file {data.name}…"):
        f: BinaryIO = cast(
            BinaryIO, tempfile.NamedTemporaryFile(prefix=".mkosi-", suffix=suffix, dir=os.path.dirname(args.output))
        )
        run([*compressor_command(compress), "--stdout", data.name], stdout=f)

    return f


def qcow2_output(args: CommandLineArguments, raw: Optional[BinaryIO]) -> Optional[BinaryIO]:
    if not args.output_format.is_disk():
        return raw
    assert raw is not None

    if not args.qcow2:
        return raw

    with complete_step("Converting image file to qcow2…"):
        f: BinaryIO = cast(BinaryIO, tempfile.NamedTemporaryFile(prefix=".mkosi-", dir=os.path.dirname(args.output)))
        run(["qemu-img", "convert", "-onocow=on", "-fraw", "-Oqcow2", raw.name, f.name])

    return f


def write_root_hash_file(args: CommandLineArguments, root_hash: Optional[str]) -> Optional[BinaryIO]:
    if root_hash is None:
        return None

    assert args.output_root_hash_file is not None

    suffix = roothash_suffix(args.usr_only)
    with complete_step(f"Writing {suffix} file…"):
        f: BinaryIO = cast(
            BinaryIO,
            tempfile.NamedTemporaryFile(mode="w+b", prefix=".mkosi", dir=os.path.dirname(args.output_root_hash_file)),
        )
        f.write((root_hash + "\n").encode())

    return f


def copy_nspawn_settings(args: CommandLineArguments) -> Optional[BinaryIO]:
    if args.nspawn_settings is None:
        return None

    assert args.output_nspawn_settings is not None

    with complete_step("Copying nspawn settings file…"):
        f: BinaryIO = cast(
            BinaryIO,
            tempfile.NamedTemporaryFile(
                mode="w+b", prefix=".mkosi-", dir=os.path.dirname(args.output_nspawn_settings)
            ),
        )

        with open(args.nspawn_settings, "rb") as c:
            f.write(c.read())

    return f


def hash_file(of: TextIO, sf: BinaryIO, fname: str) -> None:
    bs = 16 * 1024 ** 2
    h = hashlib.sha256()

    sf.seek(0)
    buf = sf.read(bs)
    while len(buf) > 0:
        h.update(buf)
        buf = sf.read(bs)

    of.write(h.hexdigest() + " *" + fname + "\n")


def calculate_sha256sum(
    args: CommandLineArguments,
    raw: Optional[BinaryIO],
    archive: Optional[BinaryIO],
    root_hash_file: Optional[BinaryIO],
    split_root: Optional[BinaryIO],
    split_verity: Optional[BinaryIO],
    split_kernel: Optional[BinaryIO],
    nspawn_settings: Optional[BinaryIO],
) -> Optional[TextIO]:
    if args.output_format in (OutputFormat.directory, OutputFormat.subvolume):
        return None

    if not args.checksum:
        return None

    assert args.output_checksum is not None

    with complete_step("Calculating SHA256SUMS…"):
        f: TextIO = cast(
            TextIO,
            tempfile.NamedTemporaryFile(
                mode="w+", prefix=".mkosi-", encoding="utf-8", dir=os.path.dirname(args.output_checksum)
            ),
        )

        if raw is not None:
            hash_file(f, raw, os.path.basename(args.output))
        if archive is not None:
            hash_file(f, archive, os.path.basename(args.output))
        if root_hash_file is not None:
            assert args.output_root_hash_file is not None
            hash_file(f, root_hash_file, os.path.basename(args.output_root_hash_file))
        if split_root is not None:
            assert args.output_split_root is not None
            hash_file(f, split_root, os.path.basename(args.output_split_root))
        if split_verity is not None:
            assert args.output_split_verity is not None
            hash_file(f, split_verity, os.path.basename(args.output_split_verity))
        if split_kernel is not None:
            assert args.output_split_kernel is not None
            hash_file(f, split_kernel, os.path.basename(args.output_split_kernel))
        if nspawn_settings is not None:
            assert args.output_nspawn_settings is not None
            hash_file(f, nspawn_settings, os.path.basename(args.output_nspawn_settings))

        f.flush()

    return f


def calculate_signature(args: CommandLineArguments, checksum: Optional[IO[Any]]) -> Optional[BinaryIO]:
    if not args.sign:
        return None

    if checksum is None:
        return None

    assert args.output_signature is not None

    with complete_step("Signing SHA256SUMS…"):
        f: BinaryIO = cast(
            BinaryIO,
            tempfile.NamedTemporaryFile(mode="wb", prefix=".mkosi-", dir=os.path.dirname(args.output_signature)),
        )

        cmdline = ["gpg", "--detach-sign"]

        if args.key is not None:
            cmdline += ["--default-key", args.key]

        checksum.seek(0)
        run(cmdline, stdin=checksum, stdout=f)

    return f


def calculate_bmap(args: CommandLineArguments, raw: Optional[BinaryIO]) -> Optional[TextIO]:
    if not args.bmap:
        return None

    if not args.output_format.is_disk_rw():
        return None
    assert raw is not None
    assert args.output_bmap is not None

    with complete_step("Creating BMAP file…"):
        f: TextIO = cast(
            TextIO,
            tempfile.NamedTemporaryFile(
                mode="w+", prefix=".mkosi-", encoding="utf-8", dir=os.path.dirname(args.output_bmap)
            ),
        )

        cmdline = ["bmaptool", "create", raw.name]
        run(cmdline, stdout=f)

    return f


def save_cache(args: CommandLineArguments, root: Path, raw: Optional[str], cache_path: Optional[Path]) -> None:
    disk_rw = args.output_format.is_disk_rw()
    if disk_rw:
        if raw is None or cache_path is None:
            return
    else:
        if cache_path is None:
            return

    with complete_step("Installing cache copy…", f"Installed cache copy {path_relative_to_cwd(cache_path)}"):

        if disk_rw:
            assert raw is not None
            os.chmod(raw, 0o666 & ~args.original_umask)
            shutil.move(raw, cache_path)
        else:
            unlink_try_hard(cache_path)
            shutil.move(cast(str, root), cache_path)  # typing bug, .move() accepts Path


def _link_output(args: CommandLineArguments, oldpath: PathString, newpath: PathString) -> None:
    assert oldpath is not None
    assert newpath is not None

    os.chmod(oldpath, 0o666 & ~args.original_umask)
    os.link(oldpath, newpath)
    if args.no_chown:
        return

    sudo_uid = os.getenv("SUDO_UID")
    sudo_gid = os.getenv("SUDO_GID")
    if not (sudo_uid and sudo_gid):
        return

    relpath = path_relative_to_cwd(newpath)

    sudo_user = os.getenv("SUDO_USER", default=sudo_uid)
    with complete_step(
        f"Changing ownership of output file {relpath} to user {sudo_user} (acquired from sudo)…",
        f"Changed ownership of {relpath}",
    ):
        os.chown(newpath, int(sudo_uid), int(sudo_gid))


def link_output(args: CommandLineArguments, root: Path, artifact: Optional[BinaryIO]) -> None:
    with complete_step("Linking image file…", f"Linked {path_relative_to_cwd(args.output)}"):
        if args.output_format in (OutputFormat.directory, OutputFormat.subvolume):
            assert artifact is None

            make_read_only(args, root, for_cache=False, b=False)
            os.rename(root, args.output)
            make_read_only(args, args.output, for_cache=False, b=True)

        elif args.output_format.is_disk() or args.output_format in (
            OutputFormat.plain_squashfs,
            OutputFormat.tar,
            OutputFormat.cpio,
        ):
            assert artifact is not None
            _link_output(args, artifact.name, args.output)


def link_output_nspawn_settings(args: CommandLineArguments, path: Optional[SomeIO]) -> None:
    if path:
        assert args.output_nspawn_settings
        with complete_step(
            "Linking nspawn settings file…", f"Linked {path_relative_to_cwd(args.output_nspawn_settings)}"
        ):
            _link_output(args, path.name, args.output_nspawn_settings)


def link_output_checksum(args: CommandLineArguments, checksum: Optional[SomeIO]) -> None:
    if checksum:
        assert args.output_checksum
        with complete_step("Linking SHA256SUMS file…", f"Linked {path_relative_to_cwd(args.output_checksum)}"):
            _link_output(args, checksum.name, args.output_checksum)


def link_output_root_hash_file(args: CommandLineArguments, root_hash_file: Optional[SomeIO]) -> None:
    if root_hash_file:
        assert args.output_root_hash_file
        suffix = roothash_suffix(args.usr_only)
        with complete_step(f"Linking {suffix} file…", f"Linked {path_relative_to_cwd(args.output_root_hash_file)}"):
            _link_output(args, root_hash_file.name, args.output_root_hash_file)


def link_output_signature(args: CommandLineArguments, signature: Optional[SomeIO]) -> None:
    if signature:
        assert args.output_signature is not None
        with complete_step("Linking SHA256SUMS.gpg file…", f"Linked {path_relative_to_cwd(args.output_signature)}"):
            _link_output(args, signature.name, args.output_signature)


def link_output_bmap(args: CommandLineArguments, bmap: Optional[SomeIO]) -> None:
    if bmap:
        assert args.output_bmap
        with complete_step("Linking .bmap file…", f"Linked {path_relative_to_cwd(args.output_bmap)}"):
            _link_output(args, bmap.name, args.output_bmap)


def link_output_sshkey(args: CommandLineArguments, sshkey: Optional[SomeIO]) -> None:
    if sshkey:
        assert args.output_sshkey
        with complete_step("Linking private ssh key file…", f"Linked {path_relative_to_cwd(args.output_sshkey)}"):
            _link_output(args, sshkey.name, args.output_sshkey)
            os.chmod(args.output_sshkey, 0o600)


def link_output_split_root(args: CommandLineArguments, split_root: Optional[SomeIO]) -> None:
    if split_root:
        assert args.output_split_root
        with complete_step(
            "Linking split root file system…", f"Linked {path_relative_to_cwd(args.output_split_root)}"
        ):
            _link_output(args, split_root.name, args.output_split_root)


def link_output_split_verity(args: CommandLineArguments, split_verity: Optional[SomeIO]) -> None:
    if split_verity:
        assert args.output_split_verity
        with complete_step("Linking split Verity data…", f"Linked {path_relative_to_cwd(args.output_split_verity)}"):
            _link_output(args, split_verity.name, args.output_split_verity)


def link_output_split_kernel(args: CommandLineArguments, split_kernel: Optional[SomeIO]) -> None:
    if split_kernel:
        assert args.output_split_kernel
        with complete_step("Linking split kernel image…", f"Linked {path_relative_to_cwd(args.output_split_kernel)}"):
            _link_output(args, split_kernel.name, args.output_split_kernel)


def dir_size(path: PathString) -> int:
    dir_sum = 0
    for entry in os.scandir(path):
        if entry.is_symlink():
            # We can ignore symlinks because they either point into our tree,
            # in which case we'll include the size of target directory anyway,
            # or outside, in which case we don't need to.
            continue
        elif entry.is_file():
            dir_sum += entry.stat().st_blocks * 512
        elif entry.is_dir():
            dir_sum += dir_size(entry.path)
    return dir_sum


def save_manifest(args: CommandLineArguments, manifest: Manifest) -> None:
    if manifest.has_data():
        relpath = path_relative_to_cwd(args.output)

        if ManifestFormat.json in args.manifest_format:
            with complete_step(f"Saving manifest {relpath}.manifest"):
                f: TextIO = cast(
                    TextIO,
                    tempfile.NamedTemporaryFile(
                        mode="w+",
                        encoding="utf-8",
                        prefix=".mkosi-",
                        dir=os.path.dirname(args.output),
                    ),
                )
                with f:
                    manifest.write_json(f)
                    _link_output(args, f.name, f"{args.output}.manifest")

        if ManifestFormat.changelog in args.manifest_format:
            with complete_step(f"Saving report {relpath}.packages"):
                g: TextIO = cast(
                    TextIO,
                    tempfile.NamedTemporaryFile(
                        mode="w+",
                        encoding="utf-8",
                        prefix=".mkosi-",
                        dir=os.path.dirname(args.output),
                    ),
                )
                with g:
                    manifest.write_package_report(g)
                    _link_output(args, g.name, f"{relpath}.packages")


def print_output_size(args: CommandLineArguments) -> None:
    if args.output_format in (OutputFormat.directory, OutputFormat.subvolume):
        MkosiPrinter.print_step("Resulting image size is " + format_bytes(dir_size(args.output)) + ".")
    else:
        st = os.stat(args.output)
        size = format_bytes(st.st_size)
        space = format_bytes(st.st_blocks * 512)
        MkosiPrinter.print_step(f"Resulting image size is {size}, consumes {space}.")


def setup_package_cache(args: CommandLineArguments) -> Optional[TempDir]:
    if args.cache_path and args.cache_path.exists():
        return None

    d = None
    with complete_step("Setting up package cache…", "Setting up package cache {} complete") as output:
        if args.cache_path is None:
            d = tempfile.TemporaryDirectory(dir=os.path.dirname(args.output), prefix=".mkosi-")
            args.cache_path = Path(d.name)
        else:
            os.makedirs(args.cache_path, 0o755, exist_ok=True)
        output.append(args.cache_path)

    return d


def remove_duplicates(items: List[T]) -> List[T]:
    "Return list with any repetitions removed"
    # We use a dictionary to simulate an ordered set
    return list({x: None for x in items})


class ListAction(argparse.Action):
    delimiter: str

    def __init__(self, *args: Any, choices: Optional[Iterable[Any]] = None, **kwargs: Any) -> None:
        self.list_choices = choices
        # mypy doesn't like the following call due to https://github.com/python/mypy/issues/6799,
        # so let's, temporarily, ignore the error
        super().__init__(choices=choices, *args, **kwargs)  # type: ignore[misc]

    def __call__(
        self,  # These type-hints are copied from argparse.pyi
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Union[str, Sequence[Any], None],
        option_string: Optional[str] = None,
    ) -> None:
        ary = getattr(namespace, self.dest)
        if ary is None:
            ary = []

        if isinstance(values, str):
            # Support list syntax for comma separated lists as well
            if self.delimiter == "," and values.startswith("[") and values.endswith("]"):
                values = values[1:-1]

            # Make sure delimiters between quotes are ignored.
            # Inspired by https://stackoverflow.com/a/2787979.
            values = [x.strip() for x in re.split(f"""{self.delimiter}(?=(?:[^'"]|'[^']*'|"[^"]*")*$)""", values) if x]

        if isinstance(values, list):
            for x in values:
                if self.list_choices is not None and x not in self.list_choices:
                    raise ValueError(f"Unknown value {x!r}")

                # Remove ! prefixed list entries from list. !* removes all entries. This works for strings only now.
                if x == "!*":
                    ary = []
                elif isinstance(x, str) and x.startswith("!"):
                    if x[1:] in ary:
                        ary.remove(x[1:])
                else:
                    ary.append(x)
        else:
            ary.append(values)

        ary = remove_duplicates(ary)
        setattr(namespace, self.dest, ary)


class CommaDelimitedListAction(ListAction):
    delimiter = ","


class ColonDelimitedListAction(ListAction):
    delimiter = ":"


class SpaceDelimitedListAction(ListAction):
    delimiter = " "


class BooleanAction(argparse.Action):
    """Parse boolean command line arguments

    The argument may be added more than once. The argument may be set explicitly (--foo yes)
    or implicitly --foo. If the parameter name starts with "not-" or "without-" the value gets
    inverted.
    """

    def __init__(
        self,  # These type-hints are copied from argparse.pyi
        option_strings: Sequence[str],
        dest: str,
        nargs: Optional[Union[int, str]] = None,
        const: Any = True,
        default: Any = False,
        **kwargs: Any,
    ) -> None:
        if nargs is not None:
            raise ValueError("nargs not allowed")
        super().__init__(option_strings, dest, nargs="?", const=const, default=default, **kwargs)

    def __call__(
        self,  # These type-hints are copied from argparse.pyi
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Union[str, Sequence[Any], None, bool],
        option_string: Optional[str] = None,
    ) -> None:
        new_value = self.default
        if isinstance(values, str):
            try:
                new_value = parse_boolean(values)
            except ValueError as exp:
                raise argparse.ArgumentError(self, str(exp))
        elif isinstance(values, bool):  # Assign const
            new_value = values
        else:
            raise argparse.ArgumentError(self, "Invalid argument for %s %s" % (str(option_string), str(values)))

        # invert the value if the argument name starts with "not" or "without"
        for option in self.option_strings:
            if option[2:].startswith("not-") or option[2:].startswith("without-"):
                new_value = not new_value
                break

        setattr(namespace, self.dest, new_value)


class CleanPackageMetadataAction(BooleanAction):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Union[str, Sequence[Any], None, bool],
        option_string: Optional[str] = None,
    ) -> None:

        if isinstance(values, str) and values == "auto":
            setattr(namespace, self.dest, "auto")
        else:
            super().__call__(parser, namespace, values, option_string)


class WithNetworkAction(BooleanAction):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Union[str, Sequence[Any], None, bool],
        option_string: Optional[str] = None,
    ) -> None:

        if isinstance(values, str) and values == "never":
            setattr(namespace, self.dest, "never")
        else:
            super().__call__(parser, namespace, values, option_string)


class CustomHelpFormatter(argparse.HelpFormatter):
    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)
        default = self._get_default_metavar_for_optional(action)
        args_string = self._format_args(action, default)
        return ", ".join(action.option_strings) + " " + args_string


class ArgumentParserMkosi(argparse.ArgumentParser):
    """ArgumentParser with support for mkosi.defaults file(s)

    This derived class adds a simple ini file parser to python's ArgumentParser features.
    Each line of the ini file is converted to a command line argument. Example:
    "FooBar=Hello_World"  in the ini file appends "--foo-bar Hello_World" to sys.argv.

    Command line arguments starting with - or --are considered as regular arguments. Arguments
    starting with @ are considered as files which are fed to the ini file parser implemented
    in this class.
    """

    # Mapping of parameters supported in config files but not as command line arguments.
    SPECIAL_MKOSI_DEFAULT_PARAMS = {
        "QCow2": "--qcow2",
        "OutputDirectory": "--output-dir",
        "WorkspaceDirectory": "--workspace-dir",
        "XZ": "--compress-output=xz",
        "NSpawnSettings": "--settings",
        "ESPSize": "--esp-size",
        "CheckSum": "--checksum",
        "BMap": "--bmap",
        "Packages": "--package",
        "ExtraTrees": "--extra-tree",
        "SkeletonTrees": "--skeleton-tree",
        "BuildPackages": "--build-package",
        "PostInstallationScript": "--postinst-script",
        "GPTFirstLBA": "--gpt-first-lba",
        "TarStripSELinuxContext": "--tar-strip-selinux-context",
    }

    fromfile_prefix_chars: str = "@"

    def __init__(self, *kargs: Any, **kwargs: Any) -> None:
        self._ini_file_section = ""
        self._ini_file_key = ""  # multi line list processing
        self._ini_file_list_mode = False

        # Add config files to be parsed
        kwargs["fromfile_prefix_chars"] = ArgumentParserMkosi.fromfile_prefix_chars
        kwargs["formatter_class"] = CustomHelpFormatter

        super().__init__(*kargs, **kwargs)

    @staticmethod
    def _camel_to_arg(camel: str) -> str:
        s1 = re.sub("(.)([A-Z][a-z]+)", r"\1-\2", camel)
        return re.sub("([a-z0-9])([A-Z])", r"\1-\2", s1).lower()

    @classmethod
    def _ini_key_to_cli_arg(cls, key: str) -> str:
        return cls.SPECIAL_MKOSI_DEFAULT_PARAMS.get(key) or ("--" + cls._camel_to_arg(key))

    def _read_args_from_files(self, arg_strings: List[str]) -> List[str]:
        """Convert @ prefixed command line arguments with corresponding file content

        Regular arguments are just returned. Arguments prefixed with @ are considered as
        configuration file paths. The settings of each file are parsed and returned as
        command line arguments.
        Example:
          The following mkosi.default is loaded.
          [Distribution]
          Distribution=fedora

          mkosi is called like: mkosi -p httpd

          arg_strings: ['@mkosi.default', '-p', 'httpd']
          return value: ['--distribution', 'fedora', '-p', 'httpd']
        """

        # expand arguments referencing files
        new_arg_strings = []
        for arg_string in arg_strings:
            # for regular arguments, just add them back into the list
            if not arg_string or arg_string[0] not in self.fromfile_prefix_chars:
                new_arg_strings.append(arg_string)
                continue
            # replace arguments referencing files with the file content
            try:
                # This used to use configparser.ConfigParser before, but
                # ConfigParser's interpolation clashes with systemd style
                # specifier, e.g. %u for user, since both use % as a sigil.
                config = configparser.RawConfigParser(delimiters="=", inline_comment_prefixes=("#",))
                config.optionxform = str  # type: ignore
                with open(arg_string[1:]) as args_file:
                    config.read_file(args_file)

                # Rename old [Packages] section to [Content]
                if config.has_section("Packages") and not config.has_section("Content"):
                    config.read_dict({"Content": dict(config.items("Packages"))})
                    config.remove_section("Packages")

                for section in config.sections():
                    for key, value in config.items(section):
                        cli_arg = self._ini_key_to_cli_arg(key)

                        # \n in value strings is forwarded. Depending on the action type, \n is considered as a delimiter or needs to be replaced by a ' '
                        for action in self._actions:
                            if cli_arg in action.option_strings:
                                if isinstance(action, ListAction):
                                    value = value.replace(os.linesep, action.delimiter)
                        new_arg_strings.extend([cli_arg, value])
            except OSError as e:
                self.error(str(e))
        # return the modified argument list
        return new_arg_strings


COMPRESSION_ALGORITHMS = "zlib", "lzo", "zstd", "lz4", "xz"


def parse_compression(value: str) -> Union[str, bool]:
    if value in COMPRESSION_ALGORITHMS:
        return value
    return parse_boolean(value)


def parse_source_file_transfer(value: str) -> Optional[SourceFileTransfer]:
    if value == "":
        return None
    try:
        return SourceFileTransfer(value)
    except Exception as exp:
        raise argparse.ArgumentTypeError(str(exp))


def parse_base_packages(value: str) -> Union[str, bool]:
    if value == "conditional":
        return value
    return parse_boolean(value)


def parse_remove_files(value: str) -> List[str]:
    """Normalize paths as relative to / to ensure we don't go outside of our root."""

    # os.path.normpath() leaves leading '//' untouched, even though it normalizes '///'.
    # This follows POSIX specification, see
    # https://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap04.html#tag_04_13.
    # Let's use lstrip() to handle zero or more leading slashes correctly.
    return ["/" + os.path.normpath(p).lstrip("/") for p in value.split(",") if p]


def create_parser() -> ArgumentParserMkosi:
    parser = ArgumentParserMkosi(prog="mkosi", description="Build Bespoke OS Images", add_help=False)

    group = parser.add_argument_group("Commands")
    group.add_argument("verb", choices=MKOSI_COMMANDS, default="build", help="Operation to execute")
    group.add_argument(
        "cmdline", nargs=argparse.REMAINDER, help="The command line to use for " + str(MKOSI_COMMANDS_CMDLINE)[1:-1]
    )
    group.add_argument("-h", "--help", action="help", help="Show this help")
    group.add_argument("--version", action="version", version="%(prog)s " + __version__)

    group = parser.add_argument_group("Distribution")
    group.add_argument("-d", "--distribution", choices=Distribution.__members__, help="Distribution to install")
    group.add_argument("-r", "--release", help="Distribution release to install")
    group.add_argument("-m", "--mirror", help="Distribution mirror to use")
    group.add_argument(
        "--repositories", action=CommaDelimitedListAction, default=[], help="Repositories to use", metavar="REPOS"
    )
    group.add_argument(
        "--use-host-repositories",
        action=BooleanAction,
        help="Use host's existing software repositories (only for dnf-based distributions)",
    )
    group.add_argument("--architecture", help="Override the architecture of installation")

    group = parser.add_argument_group("Output")
    group.add_argument(
        "-t",
        "--format",
        dest="output_format",
        choices=OutputFormat,
        type=OutputFormat.from_string,
        help="Output Format",
    )
    group.add_argument(
        "--manifest-format",
        action=CommaDelimitedListAction,
        type=cast(Callable[[str], ManifestFormat], ManifestFormat.parse_list),
        help="Manifest Format",
    )
    group.add_argument(
        "-o", "--output",
        help="Output image path",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--output-split-root",
        help="Output root or /usr/ partition image path (if --split-artifacts is used)",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--output-split-verity",
        help="Output Verity partition image path (if --split-artifacts is used)",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--output-split-kernel",
        help="Output kernel path (if --split-artifacts is used)",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "-O", "--output-dir",
        help="Output root directory",
        type=Path,
        metavar="DIR",
    )
    group.add_argument(
        "--workspace-dir",
        help="Workspace directory",
        type=Path,
        metavar="DIR",
    )
    group.add_argument(
        "-f",
        "--force",
        action="count",
        dest="force_count",
        default=0,
        help="Remove existing image file before operation",
    )
    group.add_argument(
        "-b",
        "--bootable",
        action=BooleanAction,
        help="Make image bootable on EFI (only gpt_ext4, gpt_xfs, gpt_btrfs, gpt_squashfs)",
    )
    group.add_argument(
        "--boot-protocols",
        action=CommaDelimitedListAction,
        help="Boot protocols to use on a bootable image",
        metavar="PROTOCOLS",
        default=[],
    )
    group.add_argument(
        "--kernel-command-line",
        action=SpaceDelimitedListAction,
        default=["rhgb", "selinux=0", "audit=0"],
        help="Set the kernel command line (only bootable images)",
    )
    group.add_argument(
        "--kernel-commandline", action=SpaceDelimitedListAction, dest="kernel_command_line", help=argparse.SUPPRESS
    )  # Compatibility option
    group.add_argument(
        "--secure-boot", action=BooleanAction, help="Sign the resulting kernel/initrd image for UEFI SecureBoot"
    )
    group.add_argument(
        "--secure-boot-key",
        help="UEFI SecureBoot private key in PEM format",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--secure-boot-certificate",
        help="UEFI SecureBoot certificate in X509 format",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--secure-boot-valid-days",
        help="Number of days UEFI SecureBoot keys should be valid when generating keys",
        metavar="DAYS",
        default="730",
    )
    group.add_argument(
        "--secure-boot-common-name",
        help="Template for the UEFI SecureBoot CN when generating keys",
        metavar="CN",
        default="mkosi of %u",
    )
    group.add_argument(
        "--read-only",
        action=BooleanAction,
        help="Make root volume read-only (only gpt_ext4, gpt_xfs, gpt_btrfs, subvolume, implied with gpt_squashfs and plain_squashfs)",
    )
    group.add_argument(
        "--encrypt", choices=("all", "data"), help='Encrypt everything except: ESP ("all") or ESP and root ("data")'
    )
    group.add_argument("--verity", action=BooleanAction, help="Add integrity partition (implies --read-only)")
    group.add_argument(
        "--compress",
        type=parse_compression,
        nargs="?",
        metavar="ALG",
        help="Enable compression (in-fs if supported, whole-output otherwise)",
    )
    group.add_argument(
        "--compress-fs",
        type=parse_compression,
        nargs="?",
        metavar="ALG",
        help="Enable in-filesystem compression (gpt_btrfs, subvolume, gpt_squashfs, plain_squashfs)",
    )
    group.add_argument(
        "--compress-output",
        type=parse_compression,
        nargs="?",
        metavar="ALG",
        help="Enable whole-output compression (with images or archives)",
    )
    group.add_argument(
        "--mksquashfs", dest="mksquashfs_tool", type=str.split, default=[], help="Script to call instead of mksquashfs"
    )
    group.add_argument(
        "--xz",
        action="store_const",
        dest="compress_output",
        const="xz",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--qcow2",
        action=BooleanAction,
        help="Convert resulting image to qcow2 (only gpt_ext4, gpt_xfs, gpt_btrfs, gpt_squashfs)",
    )
    group.add_argument("--hostname", help="Set hostname")
    group.add_argument("--image-version", help="Set version for image")
    group.add_argument("--image-id", help="Set ID for image")
    group.add_argument(
        "--no-chown",
        action=BooleanAction,
        help="When running with sudo, disable reassignment of ownership of the generated files to the original user",
    )  # NOQA: E501
    group.add_argument(
        "--tar-strip-selinux-context",
        action=BooleanAction,
        help="Do not include SELinux file context information in tar. Not compatible with bsdtar.",
    )
    group.add_argument(
        "-i", "--incremental", action=BooleanAction, help="Make use of and generate intermediary cache images"
    )
    group.add_argument("-M", "--minimize", action=BooleanAction, help="Minimize root file system size")
    group.add_argument(
        "--without-unified-kernel-images",
        action=BooleanAction,
        dest="with_unified_kernel_images",
        default=True,
        help="Do not install unified kernel images",
    )
    group.add_argument("--with-unified-kernel-images", action=BooleanAction, default=True, help=argparse.SUPPRESS)
    group.add_argument("--gpt-first-lba", type=int, help="Set the first LBA within GPT Header", metavar="FIRSTLBA")
    group.add_argument("--hostonly-initrd", action=BooleanAction, help="Enable dracut hostonly option")
    group.add_argument(
        "--split-artifacts", action=BooleanAction, help="Generate split out root/verity/kernel images, too"
    )

    group = parser.add_argument_group("Content")
    group.add_argument(
        "--base-packages",
        type=parse_base_packages,
        default=True,
        help="Automatically inject basic packages in the system (systemd, kernel, …)",
        metavar="OPTION",
    )
    group.add_argument(
        "-p",
        "--package",
        action=CommaDelimitedListAction,
        dest="packages",
        default=[],
        help="Add an additional package to the OS image",
        metavar="PACKAGE",
    )
    group.add_argument("--with-docs", action=BooleanAction, help="Install documentation")
    group.add_argument(
        "-T",
        "--without-tests",
        action=BooleanAction,
        dest="with_tests",
        default=True,
        help="Do not run tests as part of build script, if supported",
    )
    group.add_argument(
        "--with-tests", action=BooleanAction, default=True, help=argparse.SUPPRESS
    )  # Compatibility option

    group.add_argument("--password", help="Set the root password")
    group.add_argument(
        "--password-is-hashed", action=BooleanAction, help="Indicate that the root password has already been hashed"
    )
    group.add_argument("--autologin", action=BooleanAction, help="Enable root autologin")

    group.add_argument(
        "--cache",
        dest="cache_path",
        help="Package cache path",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--extra-tree",
        action=CommaDelimitedListAction,
        dest="extra_trees",
        default=[],
        help="Copy an extra tree on top of image",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--skeleton-tree",
        action="append",
        dest="skeleton_trees",
        default=[],
        help="Use a skeleton tree to bootstrap the image before installing anything",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--clean-package-metadata",
        action=CleanPackageMetadataAction,
        help="Remove package manager database and other files",
        default='auto',
    )
    group.add_argument(
        "--remove-files",
        action=CommaDelimitedListAction,
        default=[],
        help="Remove files from built image",
        type=parse_remove_files,
        metavar="GLOB",
    )
    group.add_argument(
        "--environment",
        "-E",
        action=SpaceDelimitedListAction,
        default=[],
        help="Set an environment variable when running scripts",
        metavar="NAME[=VALUE]",
    )
    group.add_argument(
        "--build-environment",  # Compatibility option
        action=SpaceDelimitedListAction,
        default=[],
        dest="environment",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--build-sources",
        help="Path for sources to build",
        metavar="PATH",
        type=Path,
    )
    group.add_argument(
        "--build-dir",  # Compatibility option
        help=argparse.SUPPRESS,
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--build-directory",
        dest="build_dir",
        help="Path to use as persistent build directory",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--include-directory",
        dest="include_dir",
        help="Path to use as persistent include directory",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--install-directory",
        dest="install_dir",
        help="Path to use as persistent install directory",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--build-package",
        action=CommaDelimitedListAction,
        dest="build_packages",
        default=[],
        help="Additional packages needed for build script",
        metavar="PACKAGE",
    )
    group.add_argument(
        "--skip-final-phase", action=BooleanAction, help="Skip the (second) final image building phase.", default=False
    )
    group.add_argument(
        "--build-script",
        help="Build script to run inside image",
        type=script_path,
        metavar="PATH",
    )
    group.add_argument(
        "--prepare-script",
        help="Prepare script to run inside the image before it is cached",
        type=script_path,
        metavar="PATH",
    )
    group.add_argument(
        "--postinst-script",
        help="Postinstall script to run inside image",
        type=script_path,
        metavar="PATH",
    )
    group.add_argument(
        "--finalize-script",
        help="Postinstall script to run outside image",
        type=script_path,
        metavar="PATH",
    )
    group.add_argument(
        "--source-file-transfer",
        type=parse_source_file_transfer,
        choices=[*list(SourceFileTransfer), None],
        default=None,
        help="Method used to copy build sources to the build image."
        + "; ".join([f"'{k}': {v}" for k, v in SourceFileTransfer.doc().items()])
        + " (default: copy-git-others if in a git repository, otherwise copy-all)",
    )
    group.add_argument(
        "--source-file-transfer-final",
        type=parse_source_file_transfer,
        choices=[*list(SourceFileTransfer), None],
        default=None,
        help="Method used to copy build sources to the final image."
        + "; ".join([f"'{k}': {v}" for k, v in SourceFileTransfer.doc().items() if k != SourceFileTransfer.mount])
        + " (default: None)",
    )
    group.add_argument(
        "--source-resolve-symlinks",
        action=BooleanAction,
        help="If given, any symbolic links in the build sources are resolved and the file contents copied to the"
        + " build image. If not given, they are left as symbolic links in the build image."
        + " Only applies if --source-file-transfer is set to 'copy-all'. (default: keep as symbolic links)",
    )
    group.add_argument(
        "--source-resolve-symlinks-final",
        action=BooleanAction,
        help="If given, any symbolic links in the build sources are resolved and the file contents copied to the"
        + " final image. If not given, they are left as symbolic links in the final image."
        + " Only applies if --source-file-transfer-final is set to 'copy-all'. (default: keep as symbolic links)",
    )
    group.add_argument(
        "--with-network",
        action=WithNetworkAction,
        help="Run build and postinst scripts with network access (instead of private network)",
    )
    group.add_argument(
        "--settings",
        dest="nspawn_settings",
        help="Add in .nspawn settings file",
        type=Path,
        metavar="PATH",
    )

    group = parser.add_argument_group("Partitions")
    group.add_argument(
        "--root-size", help="Set size of root partition (only gpt_ext4, gpt_xfs, gpt_btrfs)", metavar="BYTES"
    )
    group.add_argument(
        "--esp-size",
        help="Set size of EFI system partition (only gpt_ext4, gpt_xfs, gpt_btrfs, gpt_squashfs)",
        metavar="BYTES",
    )
    group.add_argument(
        "--xbootldr-size",
        help="Set size of the XBOOTLDR partition (only gpt_ext4, gpt_xfs, gpt_btrfs, gpt_squashfs)",
        metavar="BYTES",
    )
    group.add_argument(
        "--swap-size",
        help="Set size of swap partition (only gpt_ext4, gpt_xfs, gpt_btrfs, gpt_squashfs)",
        metavar="BYTES",
    )
    group.add_argument(
        "--home-size", help="Set size of /home partition (only gpt_ext4, gpt_xfs, gpt_squashfs)", metavar="BYTES"
    )
    group.add_argument(
        "--srv-size", help="Set size of /srv partition (only gpt_ext4, gpt_xfs, gpt_squashfs)", metavar="BYTES"
    )
    group.add_argument(
        "--var-size", help="Set size of /var partition (only gpt_ext4, gpt_xfs, gpt_squashfs)", metavar="BYTES"
    )
    group.add_argument(
        "--tmp-size", help="Set size of /var/tmp partition (only gpt_ext4, gpt_xfs, gpt_squashfs)", metavar="BYTES"
    )
    group.add_argument(
        "--usr-only", action=BooleanAction, help="Generate a /usr/ partition instead of a root partition"
    )

    group = parser.add_argument_group("Validation (only gpt_ext4, gpt_xfs, gpt_btrfs, gpt_squashfs, tar, cpio)")
    group.add_argument("--checksum", action=BooleanAction, help="Write SHA256SUMS file")
    group.add_argument("--sign", action=BooleanAction, help="Write and sign SHA256SUMS file")
    group.add_argument("--key", help="GPG key to use for signing")
    group.add_argument(
        "--bmap",
        action=BooleanAction,
        help="Write block map file (.bmap) for bmaptool usage (only gpt_ext4, gpt_btrfs)",
    )

    group = parser.add_argument_group("Host configuration")
    group.add_argument(
        "--extra-search-path",
        dest="extra_search_paths",
        action=ColonDelimitedListAction,
        default=[],
        help="List of colon-separated paths to look for programs before looking in PATH",
    )
    group.add_argument(
        "--extra-search-paths", dest="extra_search_paths", action=ColonDelimitedListAction, help=argparse.SUPPRESS
    )  # Compatibility option
    group.add_argument("--qemu-headless", action=BooleanAction, help="Configure image for qemu's -nographic mode")
    group.add_argument("--qemu-smp", help="Configure guest's SMP settings", metavar="SMP", default="2")
    group.add_argument("--qemu-mem", help="Configure guest's RAM size", metavar="MEM", default="1G")
    group.add_argument(
        "--network-veth",
        action=BooleanAction,
        help="Create a virtual Ethernet link between the host and the container/VM",
    )
    group.add_argument(
        "--ephemeral",
        action=BooleanAction,
        help="If specified, the container/VM is run with a temporary snapshot of the output image that is "
        "removed immediately when the container/VM terminates",
    )
    group.add_argument(
        "--ssh", action=BooleanAction, help="Set up SSH access from the host to the final image via 'mkosi ssh'"
    )
    group.add_argument(
        "--ssh-key",
        type=Path,
        metavar="PATH",
        help="Use the specified private key when using 'mkosi ssh' (requires a corresponding public key)",
    )
    group.add_argument(
        "--ssh-timeout",
        metavar="SECONDS",
        type=int,
        default=0,
        help="Wait up to SECONDS seconds for the SSH connection to be available when using 'mkosi ssh'",
    )

    group = parser.add_argument_group("Additional Configuration")
    group.add_argument(
        "-C", "--directory",
        help="Change to specified directory before doing anything",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "--default",
        dest="default_path",
        help="Read configuration data from file",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "-a", "--all", action="store_true", dest="all", default=False, help="Build all settings files in mkosi.files/"
    )
    group.add_argument(
        "--all-directory",
        dest="all_directory",
        help="Specify path to directory to read settings files from",
        type=Path,
        metavar="PATH",
    )
    group.add_argument(
        "-B",
        "--auto-bump",
        action=BooleanAction,
        help="Automatically bump image version after building",
    )
    group.add_argument(
        "--debug",
        action=CommaDelimitedListAction,
        default=[],
        help="Turn on debugging output",
        choices=("run", "build-script", "workspace-command", "disk"),
    )
    try:
        import argcomplete

        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    return parser


def load_distribution(args: argparse.Namespace) -> argparse.Namespace:
    if args.distribution is not None:
        args.distribution = Distribution[args.distribution]

    if args.distribution is None or args.release is None:
        d, r = detect_distribution()

        if args.distribution is None:
            args.distribution = d

        if args.distribution == d and d != Distribution.clear and args.release is None:
            args.release = r

    if args.distribution is None:
        die("Couldn't detect distribution.")

    return args


def parse_args(argv: Optional[List[str]] = None) -> Dict[str, argparse.Namespace]:
    """Load default values from files and parse command line arguments

    Do all about default files and command line arguments parsing. If --all argument is passed
    more than one job needs to be processed. The returned tuple contains CommandLineArguments
    valid for all jobs as well as a dict containing the arguments per job.
    """
    parser = create_parser()

    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)  # make a copy 'cause we'll be modifying the list later on

    # If ArgumentParserMkosi loads settings from mkosi.default files, the settings from files
    # are converted to command line arguments. This breaks ArgumentParser's support for default
    # values of positional arguments. Make sure the verb command gets explicitly passed.
    # Insert a -- before the positional verb argument otherwise it might be considered as an argument of
    # a parameter with nargs='?'. For example mkosi -i summary would be treated as -i=summary.
    for verb in MKOSI_COMMANDS:
        try:
            v_i = argv.index(verb)
        except ValueError:
            continue

        if v_i > 0 and argv[v_i - 1] != "--":
            argv.insert(v_i, "--")
        break
    else:
        argv += ["--", "build"]

    # First run of command line arguments parsing to get the directory of mkosi.default file and the verb argument.
    args_pre_parsed, _ = parser.parse_known_args(argv)

    if args_pre_parsed.verb == "help":
        parser.print_help()
        sys.exit(0)

    # Make sure all paths are absolute and valid.
    # Relative paths are not valid yet since we are not in the final working directory yet.
    if args_pre_parsed.directory is not None:
        directory = args_pre_parsed.directory = args_pre_parsed.directory.absolute()
    else:
        directory = Path.cwd()

    # Note that directory will be ignored if .all_directory or .default_path are absolute
    all_directory = directory / (args_pre_parsed.all_directory or "mkosi.files")
    default_path = directory / (args_pre_parsed.default_path or "mkosi.default")
    if args_pre_parsed.default_path and not default_path.exists():
        die(f"No config file found at {default_path}")

    if args_pre_parsed.all and args_pre_parsed.default_path:
        die("--all and --default= may not be combined.")

    # Parse everything in --all mode
    args_all = {}
    if args_pre_parsed.all:
        if not os.path.isdir(all_directory):
            die(f"all-directory {all_directory} does not exist")
        for f in os.scandir(all_directory):
            if not f.name.startswith("mkosi."):
                continue
            args = parse_args_file(argv, Path(f.path))
            args_all[f.name] = args
    # Parse everything in normal mode
    else:
        args = parse_args_file_group(argv, os.fspath(default_path))

        args = load_distribution(args)

        if args.distribution:
            # Parse again with any extra distribution files included.
            args = parse_args_file_group(argv, os.fspath(default_path), args.distribution)

        args_all["default"] = args

    return args_all


def parse_args_file(argv: List[str], default_path: Path) -> argparse.Namespace:
    """Parse just one mkosi.* file (--all mode)."""

    # Parse all parameters handled by mkosi.
    # Parameters forwarded to subprocesses such as nspawn or qemu end up in cmdline_argv.
    argv = argv[:1] + [f"{ArgumentParserMkosi.fromfile_prefix_chars}{default_path}"] + argv[1:]

    return create_parser().parse_args(argv)


def parse_args_file_group(
    argv: List[str], default_path: str, distribution: Optional[Distribution] = None
) -> argparse.Namespace:
    """Parse a set of mkosi.default and mkosi.default.d/* files."""
    # Add the @ prefixed filenames to current argument list in inverse priority order.
    defaults_files = []

    if os.path.isfile(default_path):
        defaults_files += [f"{ArgumentParserMkosi.fromfile_prefix_chars}{default_path}"]

    defaults_dir = "mkosi.default.d"
    if os.path.isdir(defaults_dir):
        for file in sorted(os.listdir(defaults_dir)):
            path = os.path.join(defaults_dir, file)
            if os.path.isfile(path):
                defaults_files += [f"{ArgumentParserMkosi.fromfile_prefix_chars}{path}"]

    if distribution is not None:
        distribution_dir = f"mkosi.default.d/{distribution}"
        if os.path.isdir(distribution_dir):
            for subdir in sorted(os.listdir(distribution_dir)):
                path = os.path.join(distribution_dir, subdir)
                if os.path.isfile(path):
                    defaults_files += [f"{ArgumentParserMkosi.fromfile_prefix_chars}{path}"]

    # Parse all parameters handled by mkosi.
    # Parameters forwarded to subprocesses such as nspawn or qemu end up in cmdline_argv.
    return create_parser().parse_args(defaults_files + argv)


def parse_bytes(num_bytes: Optional[str]) -> Optional[int]:
    if num_bytes is None:
        return num_bytes

    if num_bytes.endswith("G"):
        factor = 1024 ** 3
    elif num_bytes.endswith("M"):
        factor = 1024 ** 2
    elif num_bytes.endswith("K"):
        factor = 1024
    else:
        factor = 1

    if factor > 1:
        num_bytes = num_bytes[:-1]

    result = int(num_bytes) * factor
    if result <= 0:
        raise ValueError("Size out of range")

    if result % 512 != 0:
        raise ValueError("Size not a multiple of 512")

    return result


def detect_distribution() -> Tuple[Optional[Distribution], Optional[str]]:
    try:
        os_release = read_os_release()
    except FileNotFoundError:
        return None, None

    dist_id = os_release.get("ID", "linux")
    dist_id_like = os_release.get("ID_LIKE", "").split()
    version = os_release.get("VERSION", None)
    version_id = os_release.get("VERSION_ID", None)
    version_codename = os_release.get("VERSION_CODENAME", None)
    extracted_codename = None

    if version:
        # extract Debian release codename
        m = re.search(r"\((.*?)\)", version)
        if m:
            extracted_codename = m.group(1)

    if dist_id == "clear-linux-os":
        dist_id = "clear"

    d: Optional[Distribution] = None
    for the_id in [dist_id, *dist_id_like]:
        d = Distribution.__members__.get(the_id, None)
        if d is not None:
            break

    if d in {Distribution.debian, Distribution.ubuntu} and (version_codename or extracted_codename):
        # debootstrap needs release codenames, not version numbers
        version_id = version_codename or extracted_codename

    return d, version_id


def unlink_try_hard(path: Optional[PathString]) -> None:
    if path is None:
        return

    path = Path(path)
    try:
        return path.unlink()
    except FileNotFoundError:
        return
    except Exception:
        pass

    if shutil.which("btrfs"):
        try:
            btrfs_subvol_delete(path)
            return
        except Exception:
            pass

    shutil.rmtree(path)


def remove_glob(*patterns: PathString) -> None:
    pathgen = (glob.glob(str(pattern)) for pattern in patterns)
    paths: Set[str] = set(sum(pathgen, []))  # uniquify
    for path in paths:
        unlink_try_hard(Path(path))


def empty_directory(path: Path) -> None:
    try:
        for f in os.listdir(path):
            unlink_try_hard(path / f)
    except FileNotFoundError:
        pass


def unlink_output(args: CommandLineArguments) -> None:
    if not args.force and args.verb != "clean":
        return

    if not args.skip_final_phase:
        with complete_step("Removing output files…"):
            unlink_try_hard(args.output)
            unlink_try_hard(f"{args.output}.manifest")
            unlink_try_hard(f"{args.output}.packages")

            if args.checksum:
                unlink_try_hard(args.output_checksum)

            if args.verity:
                unlink_try_hard(args.output_root_hash_file)

            if args.sign:
                unlink_try_hard(args.output_signature)

            if args.bmap:
                unlink_try_hard(args.output_bmap)

            if args.split_artifacts:
                unlink_try_hard(args.output_split_root)
                unlink_try_hard(args.output_split_verity)
                unlink_try_hard(args.output_split_kernel)

            if args.nspawn_settings is not None:
                unlink_try_hard(args.output_nspawn_settings)

        if args.ssh and args.output_sshkey is not None:
            unlink_try_hard(args.output_sshkey)

    # We remove any cached images if either the user used --force
    # twice, or he/she called "clean" with it passed once. Let's also
    # remove the downloaded package cache if the user specified one
    # additional "--force".

    if args.verb == "clean":
        remove_build_cache = args.force_count > 0
        remove_package_cache = args.force_count > 1
    else:
        remove_build_cache = args.force_count > 1
        remove_package_cache = args.force_count > 2

    if remove_build_cache:
        if args.cache_pre_dev is not None or args.cache_pre_inst is not None:
            with complete_step("Removing incremental cache files…"):
                if args.cache_pre_dev is not None:
                    unlink_try_hard(args.cache_pre_dev)

                if args.cache_pre_inst is not None:
                    unlink_try_hard(args.cache_pre_inst)

        if args.build_dir is not None:
            with complete_step("Clearing out build directory…"):
                empty_directory(args.build_dir)

        if args.include_dir is not None:
            with complete_step("Clearing out include directory…"):
                empty_directory(args.include_dir)

        if args.install_dir is not None:
            with complete_step("Clearing out install directory…"):
                empty_directory(args.install_dir)

    if remove_package_cache:
        if args.cache_path is not None:
            with complete_step("Clearing out package cache…"):
                empty_directory(args.cache_path)


def parse_boolean(s: str) -> bool:
    "Parse 1/true/yes as true and 0/false/no as false"
    s_l = s.lower()
    if s_l in {"1", "true", "yes"}:
        return True

    if s_l in {"0", "false", "no"}:
        return False

    raise ValueError(f"Invalid literal for bool(): {s!r}")


def find_nspawn_settings(args: argparse.Namespace) -> None:
    if args.nspawn_settings is not None:
        return

    if os.path.exists("mkosi.nspawn"):
        args.nspawn_settings = "mkosi.nspawn"


def find_extra(args: argparse.Namespace) -> None:

    if len(args.extra_trees) > 0:
        return

    if os.path.isdir("mkosi.extra"):
        args.extra_trees.append(Path("mkosi.extra"))
    if os.path.isfile("mkosi.extra.tar"):
        args.extra_trees.append(Path("mkosi.extra.tar"))


def find_skeleton(args: argparse.Namespace) -> None:

    if len(args.skeleton_trees) > 0:
        return

    if os.path.isdir("mkosi.skeleton"):
        args.skeleton_trees.append(Path("mkosi.skeleton"))
    if os.path.isfile("mkosi.skeleton.tar"):
        args.skeleton_trees.append(Path("mkosi.skeleton.tar"))


def args_find_path(args: argparse.Namespace, name: str, path: str, *, as_list: bool = False) -> None:
    if getattr(args, name) is not None:
        return
    abspath = Path(path).absolute()
    if abspath.exists():
        setattr(args, name, [abspath] if as_list else abspath)


def find_cache(args: argparse.Namespace) -> None:
    if args.cache_path is not None:
        return

    if os.path.exists("mkosi.cache/"):
        dirname = args.distribution.name

        # Clear has a release number that can be used, however the
        # cache is valid (and more efficient) across releases.
        if args.distribution != Distribution.clear and args.release is not None:
            dirname += "~" + args.release

        args.cache_path = Path("mkosi.cache", dirname)


def require_private_file(name: str, description: str) -> None:
    mode = os.stat(name).st_mode & 0o777
    if mode & 0o007:
        warn(dedent(f"""\
            Permissions of '{name}' of '{mode:04o}' are too open.
            When creating {description} files use an access mode that restricts access to the owner only.
        """))


def find_passphrase(args: argparse.Namespace) -> None:
    if args.encrypt is None:
        args.passphrase = None
        return

    try:
        require_private_file("mkosi.passphrase", "passphrase")

        args.passphrase = {"type": "file", "content": "mkosi.passphrase"}

    except FileNotFoundError:
        while True:
            passphrase = getpass.getpass("Please enter passphrase: ")
            passphrase_confirmation = getpass.getpass("Passphrase confirmation: ")
            if passphrase == passphrase_confirmation:
                args.passphrase = {"type": "stdin", "content": passphrase}
                break

            MkosiPrinter.info("Passphrase doesn't match confirmation. Please try again.")


def find_password(args: argparse.Namespace) -> None:
    if args.password is not None:
        return

    try:
        require_private_file("mkosi.rootpw", "root password")

        with open("mkosi.rootpw") as f:
            args.password = f.read().strip()

    except FileNotFoundError:
        pass


def find_secure_boot(args: argparse.Namespace) -> None:
    if not args.secure_boot:
        return

    if args.secure_boot_key is None:
        if os.path.exists("mkosi.secure-boot.key"):
            args.secure_boot_key = Path("mkosi.secure-boot.key")

    if args.secure_boot_certificate is None:
        if os.path.exists("mkosi.secure-boot.crt"):
            args.secure_boot_certificate = Path("mkosi.secure-boot.crt")


def find_image_version(args: argparse.Namespace) -> None:
    if args.image_version is not None:
        return

    try:
        with open("mkosi.version") as f:
            args.image_version = f.read().strip()
    except FileNotFoundError:
        pass


KNOWN_SUFFIXES = {
    ".xz",
    ".zstd",
    ".raw",
    ".tar",
    ".cpio",
    ".qcow2",
}


def strip_suffixes(path: Path) -> Path:
    while path.suffix in KNOWN_SUFFIXES:
        path = path.with_suffix("")
    return path


def xescape(s: str) -> str:
    "Escape a string udev-style, for inclusion in /dev/disk/by-*/* symlinks"

    ret = ""
    for c in s:
        if ord(c) <= 32 or ord(c) >= 127 or c == "/":
            ret = ret + "\\x%02x" % ord(c)
        else:
            ret = ret + str(c)

    return ret


def build_auxiliary_output_path(args: argparse.Namespace, suffix: str, can_compress: bool = False) -> Path:
    output = strip_suffixes(args.output)
    compression = should_compress_output(args) if can_compress else False
    return output.with_name(f"{output.name}{suffix}{compression or ''}")


DISABLED = Path('DISABLED')  # A placeholder value to suppress autodetection.
                             # This is used as a singleton, i.e. should be compared with
                             # 'is' in other parts of the code.

def script_path(value: Optional[str]) -> Optional[Path]:
    if value is None:
        return None
    if value == '':
        return DISABLED
    return Path(value)


def normalize_script(path: Optional[Path]) -> Optional[Path]:
    if not path or path is DISABLED:
        return None
    path = Path(path).absolute()
    if not path.exists():
        die(f"{path} does not exist")
    if not path.is_file():
        die(f"{path} is not a file")
    if not os.access(path, os.X_OK):
        die(f"{path} is not executable")
    return path


def load_args(args: argparse.Namespace) -> CommandLineArguments:
    global ARG_DEBUG
    ARG_DEBUG.update(args.debug)

    args_find_path(args, "nspawn_settings", "mkosi.nspawn")
    args_find_path(args, "build_script", "mkosi.build")
    args_find_path(args, "build_sources", ".")
    args_find_path(args, "build_dir", "mkosi.builddir/")
    args_find_path(args, "include_dir", "mkosi.includedir/")
    args_find_path(args, "install_dir", "mkosi.installdir/")
    args_find_path(args, "postinst_script", "mkosi.postinst")
    args_find_path(args, "prepare_script", "mkosi.prepare")
    args_find_path(args, "finalize_script", "mkosi.finalize")
    args_find_path(args, "output_dir", "mkosi.output/")
    args_find_path(args, "workspace_dir", "mkosi.workspace/")
    args_find_path(args, "mksquashfs_tool", "mkosi.mksquashfs-tool", as_list=True)

    find_extra(args)
    find_skeleton(args)
    find_password(args)
    find_passphrase(args)
    find_secure_boot(args)
    find_image_version(args)

    args.extra_search_paths = expand_paths(args.extra_search_paths)

    if args.cmdline and args.verb not in MKOSI_COMMANDS_CMDLINE:
        die("Additional parameters only accepted for " + str(MKOSI_COMMANDS_CMDLINE)[1:-1] + " invocations.")

    args.force = args.force_count > 0

    if args.output_format is None:
        args.output_format = OutputFormat.gpt_ext4

    args = load_distribution(args)

    if args.release is None:
        if args.distribution == Distribution.fedora:
            args.release = "34"
        elif args.distribution in (Distribution.centos, Distribution.centos_epel):
            args.release = "8"
        elif args.distribution in (Distribution.rocky, Distribution.rocky_epel):
            args.release = "8"
        elif args.distribution in (Distribution.alma, Distribution.alma_epel):
            args.release = "8"
        elif args.distribution == Distribution.mageia:
            args.release = "7"
        elif args.distribution == Distribution.debian:
            args.release = "unstable"
        elif args.distribution == Distribution.ubuntu:
            args.release = "focal"
        elif args.distribution == Distribution.opensuse:
            args.release = "tumbleweed"
        elif args.distribution == Distribution.clear:
            args.release = "latest"
        elif args.distribution == Distribution.photon:
            args.release = "3.0"
        elif args.distribution == Distribution.openmandriva:
            args.release = "cooker"
        else:
            args.release = "rolling"

    if args.bootable:
        if args.output_format in (
            OutputFormat.directory,
            OutputFormat.subvolume,
            OutputFormat.tar,
            OutputFormat.cpio,
            OutputFormat.plain_squashfs,
        ):
            die("Directory, subvolume, tar, cpio, and plain squashfs images cannot be booted.")

        if not args.boot_protocols:
            args.boot_protocols = ["uefi"]

            if args.distribution == Distribution.photon:
                args.boot_protocols = ["bios"]

        if not {"uefi", "bios"}.issuperset(args.boot_protocols):
            die("Not a valid boot protocol")

        if "uefi" in args.boot_protocols and args.distribution == Distribution.photon:
            die(f"uefi boot not supported for {args.distribution}")

    if args.distribution in (Distribution.centos, Distribution.centos_epel):
        epel_release = int(args.release.split(".")[0])
        if epel_release <= 8 and args.output_format == OutputFormat.gpt_btrfs:
            die(f"Sorry, CentOS {epel_release} does not support btrfs")
        if epel_release <= 7 and args.bootable and "uefi" in args.boot_protocols and args.with_unified_kernel_images:
            die(
                f"Sorry, CentOS {epel_release} does not support unified kernel images. "
                "You must use --without-unified-kernel-images."
            )

    if args.distribution in (Distribution.rocky, Distribution.rocky_epel):
        epel_release = int(args.release.split(".")[0])
        if epel_release == 8 and args.output_format == OutputFormat.gpt_btrfs:
            die(f"Sorry, Rocky {epel_release} does not support btrfs")

    if args.distribution in (Distribution.alma, Distribution.alma_epel):
        epel_release = int(args.release.split(".")[0])
        if epel_release == 8 and args.output_format == OutputFormat.gpt_btrfs:
            die(f"Sorry, Alma {epel_release} does not support btrfs")

    # Remove once https://github.com/clearlinux/clr-boot-manager/pull/238 is merged and available.
    if args.distribution == Distribution.clear and args.output_format == OutputFormat.gpt_btrfs:
        die("Sorry, Clear Linux does not support btrfs")

    if args.distribution == Distribution.clear and "," in args.boot_protocols:
        die("Sorry, Clear Linux does not support hybrid BIOS/UEFI images")

    if shutil.which("bsdtar") and args.distribution == Distribution.openmandriva and args.tar_strip_selinux_context:
        die("Sorry, bsdtar on OpenMandriva is incompatible with --tar-strip-selinux-context")

    find_cache(args)

    if args.mirror is None:
        if args.distribution in (Distribution.fedora, Distribution.centos):
            args.mirror = None
        elif args.distribution == Distribution.debian:
            args.mirror = "http://deb.debian.org/debian"
        elif args.distribution == Distribution.ubuntu:
            args.mirror = "http://archive.ubuntu.com/ubuntu"
            if platform.machine() == "aarch64":
                args.mirror = "http://ports.ubuntu.com/"
        elif args.distribution == Distribution.arch and platform.machine() == "aarch64":
            args.mirror = "http://mirror.archlinuxarm.org"
        elif args.distribution == Distribution.opensuse:
            args.mirror = "http://download.opensuse.org"
        elif args.distribution in (Distribution.rocky, Distribution.rocky_epel):
            args.mirror = None
        elif args.distribution in (Distribution.alma, Distribution.alma_epel):
            args.mirror = None

    if args.minimize and not args.output_format.can_minimize():
        die("Minimal file systems only supported for ext4 and btrfs.")

    if is_generated_root(args) and args.incremental:
        die("Sorry, incremental mode is currently not supported for squashfs or minimized file systems.")

    if args.encrypt is not None:
        if not args.output_format.is_disk():
            die("Encryption is only supported for disk images.")

        if args.encrypt == "data" and args.output_format == OutputFormat.gpt_btrfs:
            die("'data' encryption mode not supported on btrfs, use 'all' instead.")

        if args.encrypt == "all" and args.verity:
            die("'all' encryption mode may not be combined with Verity.")

    if args.sign:
        args.checksum = True

    if args.output is None:
        iid = args.image_id if args.image_id is not None else "image"
        prefix = f"{iid}_{args.image_version}" if args.image_version is not None else iid

        if args.output_format.is_disk():
            compress = should_compress_output(args)
            output = prefix + (".qcow2" if args.qcow2 else ".raw") + (f".{compress}" if compress else "")
        elif args.output_format == OutputFormat.tar:
            output = f"{prefix}.tar.xz"
        elif args.output_format == OutputFormat.cpio:
            output = f"{prefix}.cpio" + (f".{args.compress}" if args.compress else "")
        else:
            output = prefix
        args.output = Path(output)

    if args.manifest_format is None:
        args.manifest_format = [ManifestFormat.json]

    if args.output_dir is not None:
        args.output_dir = args.output_dir.absolute()

        if "/" not in str(args.output):
            args.output = args.output_dir / args.output
        else:
            warn("Ignoring configured output directory as output file is a qualified path.")

    if args.incremental or args.verb == "clean":
        if args.image_id is not None:
            # If the image ID is specified, use cache file names that are independent of the image versions, so that
            # rebuilding and bumping versions is cheap and reuses previous versions if cached.
            if args.output_dir:
                args.cache_pre_dev = args.output_dir / f"{args.image_id}.cache-pre-dev"
                args.cache_pre_inst = args.output_dir / f"{args.image_id}.cache-pre-inst"
            else:
                args.cache_pre_dev = Path(f"{args.image_id}.cache-pre-dev")
                args.cache_pre_inst = Path(f"{args.image_id}.cache-pre-inst")
        else:
            # Otherwise, derive the cache file names directly from the output file names.
            args.cache_pre_dev = Path(f"{args.output}.cache-pre-dev")
            args.cache_pre_inst = Path(f"{args.output}.cache-pre-inst")
    else:
        args.cache_pre_dev = None
        args.cache_pre_inst = None

    args.output = args.output.absolute()

    if args.output_format == OutputFormat.tar:
        args.compress_output = "xz"
    if not args.output_format.is_disk():
        args.split_artifacts = False

    if args.output_format.is_squashfs():
        args.read_only = True
        args.root_size = None
        if args.compress is False:
            die("Cannot disable compression with squashfs")
        if args.compress is None:
            args.compress = True

    if args.verity:
        args.read_only = True
        args.output_root_hash_file = build_auxiliary_output_path(args, roothash_suffix(args.usr_only))

    if args.checksum:
        args.output_checksum = args.output.with_name("SHA256SUMS")

    if args.sign:
        args.output_signature = args.output.with_name("SHA256SUMS.gpg")

    if args.bmap:
        args.output_bmap = build_auxiliary_output_path(args, ".bmap")

    if args.nspawn_settings is not None:
        args.nspawn_settings = args.nspawn_settings.absolute()
        args.output_nspawn_settings = build_auxiliary_output_path(args, ".nspawn")

    # We want this set even if --ssh is not specified so we can find the SSH key when verb == "ssh".
    if args.ssh_key is None:
        args.output_sshkey = args.output.with_name("id_rsa")

    if args.split_artifacts:
        args.output_split_root = build_auxiliary_output_path(args, ".usr" if args.usr_only else ".root", True)
        if args.verity:
            args.output_split_verity = build_auxiliary_output_path(args, ".verity", True)
        if args.bootable:
            args.output_split_kernel = build_auxiliary_output_path(args, ".efi", True)

    if args.build_sources is not None:
        args.build_sources = args.build_sources.absolute()

    if args.build_dir is not None:
        args.build_dir = args.build_dir.absolute()

    if args.include_dir is not None:
        args.include_dir = args.include_dir.absolute()

    if args.install_dir is not None:
        args.install_dir = args.install_dir.absolute()

    args.build_script = normalize_script(args.build_script)
    args.prepare_script = normalize_script(args.prepare_script)
    args.postinst_script = normalize_script(args.postinst_script)
    args.finalize_script = normalize_script(args.finalize_script)

    for i in range(len(args.environment)):
        if "=" not in args.environment[i]:
            value = os.getenv(args.environment[i], "")
            args.environment[i] += f"={value}"

    if args.cache_path is not None:
        args.cache_path = args.cache_path.absolute()

    if args.extra_trees:
        for i in range(len(args.extra_trees)):
            args.extra_trees[i] = args.extra_trees[i].absolute()

    if args.skeleton_trees is not None:
        for i in range(len(args.skeleton_trees)):
            args.skeleton_trees[i] = args.skeleton_trees[i].absolute()

    args.root_size = parse_bytes(args.root_size)
    args.home_size = parse_bytes(args.home_size)
    args.srv_size = parse_bytes(args.srv_size)
    args.var_size = parse_bytes(args.var_size)
    args.tmp_size = parse_bytes(args.tmp_size)
    args.esp_size = parse_bytes(args.esp_size)
    args.xbootldr_size = parse_bytes(args.xbootldr_size)
    args.swap_size = parse_bytes(args.swap_size)

    if args.root_size is None:
        args.root_size = 3 * 1024 * 1024 * 1024

    if args.bootable and args.esp_size is None:
        args.esp_size = 256 * 1024 * 1024

    args.verity_size = None

    if args.secure_boot_key is not None:
        args.secure_boot_key = args.secure_boot_key.absolute()

    if args.secure_boot_certificate is not None:
        args.secure_boot_certificate = args.secure_boot_certificate.absolute()

    if args.secure_boot:
        if args.secure_boot_key is None:
            die(
                "UEFI SecureBoot enabled, but couldn't find private key. (Consider placing it in mkosi.secure-boot.key?)"
            )  # NOQA: E501

        if args.secure_boot_certificate is None:
            die(
                "UEFI SecureBoot enabled, but couldn't find certificate. (Consider placing it in mkosi.secure-boot.crt?)"
            )  # NOQA: E501

    if args.verb in ("shell", "boot"):
        opname = "acquire shell" if args.verb == "shell" else "boot"
        if args.output_format in (OutputFormat.tar, OutputFormat.cpio):
            die(f"Sorry, can't {opname} with a {args.output_format} archive.")
        if should_compress_output(args):
            die("Sorry, can't {opname} with a compressed image.")
        if args.qcow2:
            die("Sorry, can't {opname} using a qcow2 image.")

    if args.verb == "qemu":
        if not args.output_format.is_disk():
            die("Sorry, can't boot non-disk images with qemu.")

    if needs_build(args) and args.qemu_headless and not args.bootable:
        die("--qemu-headless requires --bootable")

    if args.qemu_headless and "console=ttyS0" not in args.kernel_command_line:
        args.kernel_command_line.append("console=ttyS0")

    if args.bootable and args.usr_only and not args.verity:
        # GPT auto-discovery on empty kernel command lines only looks
        # for root partitions (in order to avoid ambiguities), if we
        # shall operate without one (and only have a /usr partition)
        # we thus need to explicitly say which partition to mount.
        args.kernel_command_line.append(
            "mount.usr=/dev/disk/by-partlabel/"
            + xescape(
                root_partition_name(
                    args=None, image_id=args.image_id, image_version=args.image_version, usr_only=args.usr_only
                )
            )
        )

    if not args.read_only:
        args.kernel_command_line.append("rw")

    if is_generated_root(args) and "bios" in args.boot_protocols:
        die("Sorry, BIOS cannot be combined with --minimize or squashfs filesystems")

    if args.bootable and args.distribution in (Distribution.clear, Distribution.photon):
        die("Sorry, --bootable is not supported on this distro")

    if not args.with_unified_kernel_images and "uefi" in args.boot_protocols:
        if args.distribution in (Distribution.debian, Distribution.ubuntu, Distribution.mageia, Distribution.opensuse):
            die("Sorry, --without-unified-kernel-images is not supported in UEFI mode on this distro.")

    if args.verity and not args.with_unified_kernel_images:
        die("Sorry, --verity can only be used with unified kernel images")

    if args.source_file_transfer is None:
        if os.path.exists(".git") or args.build_sources.joinpath(".git").exists():
            args.source_file_transfer = SourceFileTransfer.copy_git_others
        else:
            args.source_file_transfer = SourceFileTransfer.copy_all

    if args.source_file_transfer_final == SourceFileTransfer.mount:
        die("Sorry, --source-file-transfer-final=mount is not supported")

    if args.skip_final_phase and args.verb != "build":
        die("--skip-final-phase can only be used when building an image using 'mkosi build'")

    if args.ssh and not args.network_veth:
        die("--ssh cannot be used without --network-veth")

    if args.ssh_timeout < 0:
        die("--ssh-timeout must be >= 0")

    args.original_umask = os.umask(0o000)

    # Let's define a fixed machine ID for all our build-time
    # runs. We'll strip it off the final image, but some build-time
    # tools (dracut...) want a fixed one, hence provide one, and
    # always the same
    args.machine_id = uuid.uuid4().hex

    return CommandLineArguments(**vars(args))


def check_output(args: CommandLineArguments) -> None:
    if args.skip_final_phase:
        return

    for f in (
        args.output,
        args.output_checksum if args.checksum else None,
        args.output_signature if args.sign else None,
        args.output_bmap if args.bmap else None,
        args.output_nspawn_settings if args.nspawn_settings is not None else None,
        args.output_root_hash_file if args.verity else None,
        args.output_sshkey if args.ssh else None,
        args.output_split_root if args.split_artifacts else None,
        args.output_split_verity if args.split_artifacts else None,
        args.output_split_kernel if args.split_artifacts else None,
    ):

        if f and f.exists():
            die(f"Output path {f} exists already. (Consider invocation with --force.)")


def yes_no(b: Optional[bool]) -> str:
    return "yes" if b else "no"


def yes_no_or(b: Union[bool, str]) -> str:
    return b if isinstance(b, str) else yes_no(b)


def format_bytes_or_disabled(sz: Optional[int]) -> str:
    if sz is None:
        return "(disabled)"

    return format_bytes(sz)


def format_bytes_or_auto(sz: Optional[int]) -> str:
    if sz is None:
        return "(automatic)"

    return format_bytes(sz)


def none_to_na(s: Optional[T]) -> Union[T, str]:
    return "n/a" if s is None else s


def none_to_no(s: Optional[T]) -> Union[T, str]:
    return "no" if s is None else s


def none_to_none(o: Optional[object]) -> str:
    return "none" if o is None else str(o)


def line_join_list(array: Sequence[PathString]) -> str:
    if not array:
        return "none"
    return "\n                            ".join(str(item) for item in array)


def print_summary(args: CommandLineArguments) -> None:
    # FIXME: normal print
    MkosiPrinter.info("COMMANDS:")
    MkosiPrinter.info("                      verb: " + args.verb)
    MkosiPrinter.info("                   cmdline: " + " ".join(args.cmdline))
    MkosiPrinter.info("\nDISTRIBUTION:")
    MkosiPrinter.info("              Distribution: " + args.distribution.name)
    MkosiPrinter.info("                   Release: " + none_to_na(args.release))
    if args.architecture:
        MkosiPrinter.info("              Architecture: " + args.architecture)
    if args.mirror is not None:
        MkosiPrinter.info("                    Mirror: " + args.mirror)
    if args.repositories is not None and len(args.repositories) > 0:
        MkosiPrinter.info("              Repositories: " + ",".join(args.repositories))
    MkosiPrinter.info("     Use Host Repositories: " + yes_no(args.use_host_repositories))
    MkosiPrinter.info("\nOUTPUT:")
    if args.hostname:
        MkosiPrinter.info("                  Hostname: " + args.hostname)
    if args.image_id is not None:
        MkosiPrinter.info("                  Image ID: " + args.image_id)
    if args.image_version is not None:
        MkosiPrinter.info("             Image Version: " + args.image_version)
    MkosiPrinter.info("             Output Format: " + args.output_format.name)
    maniformats = (" ".join(str(i) for i in args.manifest_format)) or "(none)"
    MkosiPrinter.info("          Manifest Formats: " + maniformats)
    if args.output_format.can_minimize():
        MkosiPrinter.info("                  Minimize: " + yes_no(args.minimize))
    if args.output_dir:
        MkosiPrinter.info(f"          Output Directory: {args.output_dir}")
    if args.workspace_dir:
        MkosiPrinter.info(f"       Workspace Directory: {args.workspace_dir}")
    MkosiPrinter.info(f"                    Output: {args.output}")
    MkosiPrinter.info(f"           Output Checksum: {none_to_na(args.output_checksum if args.checksum else None)}")
    MkosiPrinter.info(f"          Output Signature: {none_to_na(args.output_signature if args.sign else None)}")
    MkosiPrinter.info(f"               Output Bmap: {none_to_na(args.output_bmap if args.bmap else None)}")
    MkosiPrinter.info(f"  Generate split artifacts: {yes_no(args.split_artifacts)}")
    MkosiPrinter.info(
        f"      Output Split Root FS: {none_to_na(args.output_split_root if args.split_artifacts else None)}"
    )
    MkosiPrinter.info(
        f"       Output Split Verity: {none_to_na(args.output_split_verity if args.split_artifacts else None)}"
    )
    MkosiPrinter.info(
        f"       Output Split Kernel: {none_to_na(args.output_split_kernel if args.split_artifacts else None)}"
    )
    MkosiPrinter.info(
        f"    Output nspawn Settings: {none_to_na(args.output_nspawn_settings if args.nspawn_settings is not None else None)}"
    )
    MkosiPrinter.info(
        f"                   SSH key: {none_to_na((args.ssh_key or args.output_sshkey) if args.ssh else None)}"
    )

    MkosiPrinter.info("               Incremental: " + yes_no(args.incremental))

    MkosiPrinter.info("                 Read-only: " + yes_no(args.read_only))

    MkosiPrinter.info(" Internal (FS) Compression: " + yes_no_or(should_compress_fs(args)))
    MkosiPrinter.info("Outer (output) Compression: " + yes_no_or(should_compress_output(args)))

    if args.mksquashfs_tool:
        MkosiPrinter.info("           Mksquashfs tool: " + " ".join(map(str, args.mksquashfs_tool)))

    if args.output_format.is_disk():
        MkosiPrinter.info("                     QCow2: " + yes_no(args.qcow2))

    MkosiPrinter.info("                Encryption: " + none_to_no(args.encrypt))
    MkosiPrinter.info("                    Verity: " + yes_no(args.verity))

    if args.output_format.is_disk():
        MkosiPrinter.info("                  Bootable: " + yes_no(args.bootable))

        if args.bootable:
            MkosiPrinter.info("       Kernel Command Line: " + " ".join(args.kernel_command_line))
            MkosiPrinter.info("           UEFI SecureBoot: " + yes_no(args.secure_boot))

            if args.secure_boot:
                MkosiPrinter.info(f"       UEFI SecureBoot Key: {args.secure_boot_key}")
                MkosiPrinter.info(f"     UEFI SecureBoot Cert.: {args.secure_boot_certificate}")

            MkosiPrinter.info("            Boot Protocols: " + line_join_list(args.boot_protocols))
            MkosiPrinter.info("     Unified Kernel Images: " + yes_no(args.with_unified_kernel_images))
            MkosiPrinter.info("             GPT First LBA: " + str(args.gpt_first_lba))
            MkosiPrinter.info("           Hostonly Initrd: " + yes_no(args.hostonly_initrd))

    MkosiPrinter.info("\nCONTENT:")
    MkosiPrinter.info("                  Packages: " + line_join_list(args.packages))

    if args.distribution in (
        Distribution.fedora,
        Distribution.centos,
        Distribution.centos_epel,
        Distribution.mageia,
        Distribution.rocky,
        Distribution.rocky_epel,
        Distribution.alma,
        Distribution.alma_epel,
    ):
        MkosiPrinter.info("        With Documentation: " + yes_no(args.with_docs))

    MkosiPrinter.info("             Package Cache: " + none_to_none(args.cache_path))
    MkosiPrinter.info("               Extra Trees: " + line_join_list(args.extra_trees))
    MkosiPrinter.info("            Skeleton Trees: " + line_join_list(args.skeleton_trees))
    MkosiPrinter.info("      CleanPackageMetadata: " + yes_no_or(args.clean_package_metadata))
    if args.remove_files:
        MkosiPrinter.info("              Remove Files: " + line_join_list(args.remove_files))
    MkosiPrinter.info("              Build Script: " + none_to_none(args.build_script))
    MkosiPrinter.info("        Script Environment: " + line_join_list(args.environment))

    if args.build_script:
        MkosiPrinter.info("                 Run tests: " + yes_no(args.with_tests))

    MkosiPrinter.info("                  Password: " + ("default" if args.password is None else "set"))
    MkosiPrinter.info("                 Autologin: " + yes_no(args.autologin))

    MkosiPrinter.info("             Build Sources: " + none_to_none(args.build_sources))
    MkosiPrinter.info("      Source File Transfer: " + none_to_none(args.source_file_transfer))
    MkosiPrinter.info("Source File Transfer Final: " + none_to_none(args.source_file_transfer_final))
    MkosiPrinter.info("           Build Directory: " + none_to_none(args.build_dir))
    MkosiPrinter.info("         Include Directory: " + none_to_none(args.include_dir))
    MkosiPrinter.info("         Install Directory: " + none_to_none(args.install_dir))
    MkosiPrinter.info("            Build Packages: " + line_join_list(args.build_packages))
    MkosiPrinter.info("          Skip final phase: " + yes_no(args.skip_final_phase))
    MkosiPrinter.info("        Postinstall Script: " + none_to_none(args.postinst_script))
    MkosiPrinter.info("            Prepare Script: " + none_to_none(args.prepare_script))
    MkosiPrinter.info("           Finalize Script: " + none_to_none(args.finalize_script))
    MkosiPrinter.info("      Scripts with network: " + yes_no_or(args.with_network))
    MkosiPrinter.info("           nspawn Settings: " + none_to_none(args.nspawn_settings))

    if args.output_format.is_disk():
        MkosiPrinter.info("\nPARTITIONS:")
        MkosiPrinter.info("            Root Partition: " + format_bytes_or_auto(args.root_size))
        MkosiPrinter.info("            Swap Partition: " + format_bytes_or_disabled(args.swap_size))
        if "uefi" in args.boot_protocols:
            MkosiPrinter.info("                       ESP: " + format_bytes_or_disabled(args.esp_size))
        if "bios" in args.boot_protocols:
            MkosiPrinter.info("                      BIOS: " + format_bytes_or_disabled(BIOS_PARTITION_SIZE))
        MkosiPrinter.info("        XBOOTLDR Partition: " + format_bytes_or_disabled(args.xbootldr_size))
        MkosiPrinter.info("           /home Partition: " + format_bytes_or_disabled(args.home_size))
        MkosiPrinter.info("            /srv Partition: " + format_bytes_or_disabled(args.srv_size))
        MkosiPrinter.info("            /var Partition: " + format_bytes_or_disabled(args.var_size))
        MkosiPrinter.info("        /var/tmp Partition: " + format_bytes_or_disabled(args.tmp_size))
        MkosiPrinter.info("                 /usr only: " + yes_no(args.usr_only))

        MkosiPrinter.info("\nVALIDATION:")
        MkosiPrinter.info("                  Checksum: " + yes_no(args.checksum))
        MkosiPrinter.info("                      Sign: " + yes_no(args.sign))
        MkosiPrinter.info("                   GPG Key: " + ("default" if args.key is None else args.key))

    MkosiPrinter.info("\nHOST CONFIGURATION:")
    MkosiPrinter.info("        Extra search paths: " + line_join_list(args.extra_search_paths))
    MkosiPrinter.info("             QEMU Headless: " + yes_no(args.qemu_headless))
    MkosiPrinter.info("              Network Veth: " + yes_no(args.network_veth))


def reuse_cache_tree(
    args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool, cached: bool
) -> bool:
    """If there's a cached version of this tree around, use it and
    initialize our new root directly from it. Returns a boolean indicating
    whether we are now operating on a cached version or not."""

    if cached:
        return True

    if not args.incremental:
        return False
    if for_cache:
        return False
    if args.output_format.is_disk_rw():
        return False

    fname = args.cache_pre_dev if do_run_build_script else args.cache_pre_inst
    if fname is None:
        return False

    if fname.exists():
        with complete_step(f"Copying in cached tree {fname}…"):
            copy_path(fname, root)

    return True


def make_output_dir(args: CommandLineArguments) -> None:
    """Create the output directory if set and not existing yet"""
    if args.output_dir is None:
        return

    args.output_dir.mkdir(mode=0o755, exist_ok=True)


def make_build_dir(args: CommandLineArguments) -> None:
    """Create the build directory if set and not existing yet"""
    if args.build_dir is None:
        return

    args.build_dir.mkdir(mode=0o755, exist_ok=True)


def setup_ssh(
    args: CommandLineArguments, root: Path, do_run_build_script: bool, for_cache: bool, cached: bool
) -> Optional[TextIO]:
    if do_run_build_script or not args.ssh:
        return None

    if args.distribution in (Distribution.debian, Distribution.ubuntu):
        unit = "ssh"
    else:
        unit = "sshd"

    # We cache the enable sshd step but not the keygen step because it creates a separate file on the host
    # which introduces non-trivial issue when trying to cache it.

    if not cached:
        run(["systemctl", "--root", root, "enable", unit])

    if for_cache:
        return None

    authorized_keys = root_home(args, root) / ".ssh/authorized_keys"
    f: TextIO
    if args.ssh_key:
        f = open(args.ssh_key, mode="r", encoding="utf-8")
        copy_file(f"{args.ssh_key}.pub", authorized_keys)
    else:
        assert args.output_sshkey is not None

        f = cast(
            TextIO,
            tempfile.NamedTemporaryFile(mode="w+", prefix=".mkosi-", encoding="utf-8", dir=args.output_sshkey.parent),
        )

        with complete_step("Generating SSH key pair…"):
            # Write a 'y' to confirm to overwrite the file.
            run(
                ["ssh-keygen", "-f", f.name, "-N", args.password or "", "-C", "mkosi", "-t", "ed25519"],
                input="y\n",
                text=True,
                stdout=DEVNULL,
            )

        copy_file(f"{f.name}.pub", authorized_keys)
        os.remove(f"{f.name}.pub")

    authorized_keys.chmod(0o600)

    return f


def setup_network_veth(args: CommandLineArguments, root: Path, do_run_build_script: bool, cached: bool) -> None:
    if do_run_build_script or cached or not args.network_veth:
        return

    with complete_step("Setting up network veth…"):
        network_file = root / "etc/systemd/network/80-mkosi-network-veth.network"
        with open(network_file, "w") as f:
            # Adapted from https://github.com/systemd/systemd/blob/v247/network/80-container-host0.network
            f.write(
                dedent(
                    """\
                    [Match]
                    Virtualization=!container
                    Type=ether

                    [Network]
                    DHCP=yes
                    LinkLocalAddressing=yes
                    LLDP=yes
                    EmitLLDP=customer-bridge

                    [DHCP]
                    UseTimezone=yes
                    """
                )
            )

        os.chmod(network_file, 0o644)

        run(["systemctl", "--root", root, "enable", "systemd-networkd"])


@dataclasses.dataclass
class BuildOutput:
    raw: Optional[BinaryIO]
    archive: Optional[BinaryIO]
    root_hash: Optional[str]
    sshkey: Optional[TextIO]
    split_root: Optional[BinaryIO]
    split_verity: Optional[BinaryIO]
    split_kernel: Optional[BinaryIO]

    def raw_name(self) -> Optional[str]:
        return self.raw.name if self.raw is not None else None

    @classmethod
    def empty(cls) -> BuildOutput:
        return cls(None, None, None, None, None, None, None)


def build_image(
    args: CommandLineArguments,
    root: Path,
    *,
    manifest: Optional[Manifest] = None,
    do_run_build_script: bool,
    for_cache: bool = False,
    cleanup: bool = False,
) -> BuildOutput:
    # If there's no build script set, there's no point in executing
    # the build script iteration. Let's quit early.
    if args.build_script is None and do_run_build_script:
        return BuildOutput.empty()

    make_build_dir(args)

    raw, cached = reuse_cache_image(args, do_run_build_script, for_cache)
    if for_cache and cached:
        # Found existing cache image, exiting build_image
        return BuildOutput.empty()

    if cached:
        assert raw is not None
        refresh_partition_table(args, raw)
    else:
        raw = create_image(args, for_cache)

    with attach_image_loopback(args, raw) as loopdev:

        prepare_swap(args, loopdev, cached)
        prepare_esp(args, loopdev, cached)
        prepare_xbootldr(args, loopdev, cached)

        if loopdev is not None:
            luks_format_root(args, loopdev, do_run_build_script, cached)
            luks_format_home(args, loopdev, do_run_build_script, cached)
            luks_format_srv(args, loopdev, do_run_build_script, cached)
            luks_format_var(args, loopdev, do_run_build_script, cached)
            luks_format_tmp(args, loopdev, do_run_build_script, cached)

        with luks_setup_all(args, loopdev, do_run_build_script) as encrypted:
            prepare_root(args, encrypted.root, cached)
            prepare_home(args, encrypted.home, cached)
            prepare_srv(args, encrypted.srv, cached)
            prepare_var(args, encrypted.var, cached)
            prepare_tmp(args, encrypted.tmp, cached)

            for dev in encrypted:
                refresh_file_system(args, dev, cached)

            # Mount everything together, but let's not mount the root
            # dir if we still have to generate the root image here
            prepare_tree_root(args, root)
            with mount_image(
                args,
                root,
                loopdev,
                encrypted.without_generated_root(args),
            ):
                prepare_tree(args, root, do_run_build_script, cached)
                if do_run_build_script and args.include_dir and not cached:
                    empty_directory(args.include_dir)
                    # We do a recursive unmount of root so we don't need to explicitly unmount this mount
                    # later.
                    mount_bind(args.include_dir, root / "usr/include")

                cached_tree = reuse_cache_tree(args, root, do_run_build_script, for_cache, cached)
                install_skeleton_trees(args, root, cached_tree)
                install_distribution(args, root, do_run_build_script, cached_tree)
                install_etc_hostname(args, root, cached_tree)
                install_boot_loader(args, root, loopdev, do_run_build_script, cached_tree)
                run_prepare_script(args, root, do_run_build_script, cached_tree)
                install_build_src(args, root, do_run_build_script, for_cache)
                install_build_dest(args, root, do_run_build_script, for_cache)
                install_extra_trees(args, root, for_cache)
                set_root_password(args, root, do_run_build_script, cached_tree)
                set_serial_terminal(args, root, do_run_build_script, cached_tree)
                set_autologin(args, root, do_run_build_script, cached_tree)
                sshkey = setup_ssh(args, root, do_run_build_script, for_cache, cached_tree)
                setup_network_veth(args, root, do_run_build_script, cached_tree)
                run_postinst_script(args, root, loopdev, do_run_build_script, for_cache)

                if manifest:
                    with complete_step("Recording packages in manifest…"):
                        manifest.record_packages(root)

                if cleanup:
                    clean_package_manager_metadata(args, root)
                    remove_files(args, root)
                reset_machine_id(args, root, do_run_build_script, for_cache)
                reset_random_seed(args, root)
                run_finalize_script(args, root, do_run_build_script, for_cache)
                invoke_fstrim(args, root, do_run_build_script, for_cache)
                make_read_only(args, root, for_cache)

            generated_root = make_generated_root(args, root, for_cache)
            insert_generated_root(args, raw, loopdev, generated_root, for_cache)
            split_root = (
                (generated_root or extract_partition(args, encrypted.root, do_run_build_script, for_cache))
                if args.split_artifacts
                else None
            )

            verity, root_hash = make_verity(args, encrypted.root, do_run_build_script, for_cache)
            patch_root_uuid(args, loopdev, root_hash, for_cache)
            insert_verity(args, raw, loopdev, verity, root_hash, for_cache)
            split_verity = verity if args.split_artifacts else None

            # This time we mount read-only, as we already generated
            # the verity data, and hence really shouldn't modify the
            # image anymore.
            mount = lambda: mount_image(
                args,
                root,
                loopdev,
                encrypted.without_generated_root(args),
                root_read_only=True,
            )

            install_unified_kernel(args, root, root_hash, do_run_build_script, for_cache, cached, mount)
            secure_boot_sign(args, root, do_run_build_script, for_cache, cached, mount)
            split_kernel = (
                extract_unified_kernel(args, root, do_run_build_script, for_cache, mount)
                if args.split_artifacts
                else None
            )

    archive = make_tar(args, root, do_run_build_script, for_cache) or \
              make_cpio(args, root, do_run_build_script, for_cache)

    return BuildOutput(raw or generated_root, archive, root_hash, sshkey, split_root, split_verity, split_kernel)


def one_zero(b: bool) -> str:
    return "1" if b else "0"


def install_dir(args: CommandLineArguments, root: Path) -> Path:
    return args.install_dir or workspace(root).joinpath("dest")


def nspawn_knows_arg(arg: str) -> bool:
    return bytes("unrecognized option", "UTF-8") not in run(["systemd-nspawn", arg], stderr=PIPE, check=False).stderr


def run_build_script(args: CommandLineArguments, root: Path, raw: Optional[BinaryIO]) -> None:
    if args.build_script is None:
        return

    with complete_step("Running build script…"):
        os.makedirs(install_dir(args, root), mode=0o755, exist_ok=True)

        target = f"--directory={root}" if raw is None else f"--image={raw.name}"

        with_network = 1 if args.with_network is True else 0

        cmdline = [
            "systemd-nspawn",
            "--quiet",
            target,
            f"--uuid={args.machine_id}",
            f"--machine=mkosi-{uuid.uuid4().hex}",
            "--as-pid2",
            "--register=no",
            f"--bind={install_dir(args, root)}:/root/dest",
            f"--bind={var_tmp(root)}:/var/tmp",
            f"--setenv=WITH_DOCS={one_zero(args.with_docs)}",
            f"--setenv=WITH_TESTS={one_zero(args.with_tests)}",
            f"--setenv=WITH_NETWORK={with_network}",
            "--setenv=DESTDIR=/root/dest",
        ]

        cmdline.extend(f"--setenv={env}" for env in args.environment)

        # TODO: Use --autopipe once systemd v247 is widely available.
        console_arg = f"--console={'interactive' if sys.stdout.isatty() else 'pipe'}"
        if nspawn_knows_arg(console_arg):
            cmdline += [console_arg]

        if args.default_path is not None:
            cmdline += [f"--setenv=MKOSI_DEFAULT={args.default_path}"]

        if args.image_version is not None:
            cmdline += [f"--setenv=IMAGE_VERSION={args.image_version}"]

        if args.image_id is not None:
            cmdline += [f"--setenv=IMAGE_ID={args.image_id}"]

        cmdline += nspawn_params_for_build_sources(args, args.source_file_transfer)

        if args.build_dir is not None:
            cmdline += ["--setenv=BUILDDIR=/root/build",
                        f"--bind={args.build_dir}:/root/build"]

        if args.include_dir is not None:
            cmdline += [f"--bind={args.include_dir}:/usr/include"]

        if args.with_network is True:
            # If we're using the host network namespace, use the same resolver
            cmdline += ["--bind-ro=/etc/resolv.conf"]
        else:
            cmdline += ["--private-network"]

        if args.usr_only:
            cmdline += [f"--bind={root_home(args, root)}:/root"]

        cmdline += [f"/root/{args.build_script.name}"]
        cmdline += args.cmdline

        # build-script output goes to stdout so we can run language servers from within mkosi build-scripts.
        # See https://github.com/systemd/mkosi/pull/566 for more information.
        result = run(cmdline, stdout=sys.stdout, check=False)
        if result.returncode != 0:
            if "build-script" in ARG_DEBUG:
                run(cmdline[:-1], check=False)
            die(f"Build script returned non-zero exit code {result.returncode}.")


def need_cache_images(args: CommandLineArguments) -> bool:
    if not args.incremental:
        return False

    if args.force_count > 1:
        return True

    assert args.cache_pre_dev
    assert args.cache_pre_inst

    return not args.cache_pre_dev.exists() or not args.cache_pre_inst.exists()


def remove_artifacts(
    args: CommandLineArguments,
    root: Path,
    raw: Optional[BinaryIO],
    archive: Optional[BinaryIO],
    do_run_build_script: bool,
    for_cache: bool = False,
) -> None:
    if for_cache:
        what = "cache build"
    elif do_run_build_script:
        what = "development build"
    else:
        return

    if raw is not None:
        with complete_step(f"Removing disk image from {what}…"):
            del raw

    if archive is not None:
        with complete_step(f"Removing archive image from {what}…"):
            del archive

    with complete_step(f"Removing artifacts from {what}…"):
        unlink_try_hard(root)
        unlink_try_hard(var_tmp(root))
        if args.usr_only:
            unlink_try_hard(root_home(args, root))


def build_stuff(args: CommandLineArguments) -> Manifest:
    make_output_dir(args)
    setup_package_cache(args)
    workspace = setup_workspace(args)

    image = BuildOutput.empty()
    manifest = Manifest(args)

    # Make sure tmpfiles' aging doesn't interfere with our workspace
    # while we are working on it.
    with open_close(workspace.name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC) as dir_fd, \
         btrfs_forget_stale_devices(args):

        fcntl.flock(dir_fd, fcntl.LOCK_EX)

        root = Path(workspace.name, "root")

        # If caching is requested, then make sure we have cache images around we can make use of
        if need_cache_images(args):

            # There is no point generating a pre-dev cache image if no build script is provided
            if args.build_script:
                with complete_step("Running first (development) stage to generate cached copy…"):
                    # Generate the cache version of the build image, and store it as "cache-pre-dev"
                    image = build_image(args, root, do_run_build_script=True, for_cache=True)
                    save_cache(args, root, image.raw_name(), args.cache_pre_dev)
                    remove_artifacts(args, root, image.raw, image.archive, do_run_build_script=True)

            with complete_step("Running second (final) stage to generate cached copy…"):
                # Generate the cache version of the build image, and store it as "cache-pre-inst"
                image = build_image(args, root, do_run_build_script=False, for_cache=True)
                save_cache(args, root, image.raw_name(), args.cache_pre_inst)
                remove_artifacts(args, root, image.raw, image.archive, do_run_build_script=False)

        if args.build_script:
            with complete_step("Running first (development) stage…"):
                # Run the image builder for the first (development) stage in preparation for the build script
                image = build_image(args, root, do_run_build_script=True)

                run_build_script(args, root, image.raw)
                remove_artifacts(args, root, image.raw, image.archive, do_run_build_script=True)

        # Run the image builder for the second (final) stage
        if not args.skip_final_phase:
            with complete_step("Running second (final) stage…"):
                image = build_image(args, root, manifest=manifest, do_run_build_script=False, cleanup=True)
        else:
            MkosiPrinter.print_step("Skipping (second) final image build phase.")

        raw = qcow2_output(args, image.raw)
        raw = compress_output(args, raw)
        split_root = compress_output(args, image.split_root, ".usr" if args.usr_only else ".root")
        split_verity = compress_output(args, image.split_verity, ".verity")
        split_kernel = compress_output(args, image.split_kernel, ".efi")
        root_hash_file = write_root_hash_file(args, image.root_hash)
        settings = copy_nspawn_settings(args)
        checksum = calculate_sha256sum(args, raw, image.archive, root_hash_file,
                                       split_root, split_verity, split_kernel, settings)
        signature = calculate_signature(args, checksum)
        bmap = calculate_bmap(args, raw)

        link_output(args, root, raw or image.archive)
        link_output_root_hash_file(args, root_hash_file)
        link_output_checksum(args, checksum)
        link_output_signature(args, signature)
        link_output_bmap(args, bmap)
        link_output_nspawn_settings(args, settings)
        if args.output_sshkey is not None:
            link_output_sshkey(args, image.sshkey)
        link_output_split_root(args, split_root)
        link_output_split_verity(args, split_verity)
        link_output_split_kernel(args, split_kernel)

        if image.root_hash is not None:
            MkosiPrinter.print_step(f"Root hash is {image.root_hash}.")

        return manifest


def check_root() -> None:
    if os.getuid() != 0:
        die("Must be invoked as root.")


def check_native(args: CommandLineArguments) -> None:
    if args.architecture is not None and args.architecture != platform.machine() and args.build_script:
        die("Cannot (currently) override the architecture and run build commands")


@contextlib.contextmanager
def suppress_stacktrace() -> Generator[None, None, None]:
    try:
        yield
    except subprocess.CalledProcessError as e:
        # MkosiException is silenced in main() so it doesn't print a stacktrace.
        raise MkosiException(e)


def virt_name(args: CommandLineArguments) -> str:

    name = args.hostname or args.image_id or args.output.with_suffix("").name.partition("_")[0]
    # Shorten to 13 characters so we can prefix with ve- or vt- for the network veth ifname which is limited
    # to 16 characters.
    return name[:13]


def has_networkd_vm_vt() -> bool:
    return any(
        Path(path, "80-vm-vt.network").exists()
        for path in ("/usr/lib/systemd/network", "/lib/systemd/network", "/etc/systemd/network")
    )


def ensure_networkd(args: CommandLineArguments) -> bool:
    networkd_is_running = run(["systemctl", "is-active", "--quiet", "systemd-networkd"], check=False).returncode == 0
    if not networkd_is_running:
        warn("--network-veth requires systemd-networkd to be running to initialize the host interface "
             "of the veth link ('systemctl enable --now systemd-networkd')")
        return False

    if args.verb == "qemu" and not has_networkd_vm_vt():
        warn(dedent(r"""\
            mkosi didn't find 80-vm-vt.network. This is one of systemd's built-in
            systemd-networkd config files which configures vt-* interfaces.
            mkosi needs this file in order for --network-veth to work properly for QEMU
            virtual machines. The file likely cannot be found because the systemd version
            on the host is too old (< 246) and it isn't included yet.

            As a workaround until the file is shipped by the systemd package of your distro,
            add a network file /etc/systemd/network/80-vm-vt.network with the following
            contents:

            [Match]
            Name=vt-*
            Driver=tun

            [Network]
            # Default to using a /28 prefix, giving up to 13 addresses per VM.
            Address=0.0.0.0/28
            LinkLocalAddressing=yes
            DHCPServer=yes
            IPMasquerade=yes
            LLDP=yes
            EmitLLDP=customer-bridge
            IPv6PrefixDelegation=yes
            """
        ))
        return False

    return True


def run_shell(args: CommandLineArguments) -> None:
    if args.output_format in (OutputFormat.directory, OutputFormat.subvolume):
        target = f"--directory={args.output}"
    else:
        target = f"--image={args.output}"

    cmdline = ["systemd-nspawn", target]

    if args.read_only:
        cmdline += ["--read-only"]

    # If we copied in a .nspawn file, make sure it's actually honoured
    if args.nspawn_settings is not None:
        cmdline += ["--settings=trusted"]

    if args.verb == "boot":
        cmdline += ["--boot"]

    if is_generated_root(args) or args.verity:
        cmdline += ["--volatile=overlay"]

    if args.network_veth:
        if ensure_networkd(args):
            cmdline += ["--network-veth"]

    if args.ephemeral:
        cmdline += ["--ephemeral"]

    cmdline += ["--machine", virt_name(args)]

    if args.cmdline:
        # If the verb is 'shell', args.cmdline contains the command to run.
        # Otherwise, the verb is 'boot', and we assume args.cmdline contains nspawn arguments.
        if args.verb == "shell":
            cmdline += ["--"]
        cmdline += args.cmdline

    with suppress_stacktrace():
        run(cmdline, stdout=sys.stdout, stderr=sys.stderr)


def find_qemu_binary() -> str:
    ARCH_BINARIES = {"x86_64": "qemu-system-x86_64", "i386": "qemu-system-i386"}
    arch_binary = ARCH_BINARIES.get(platform.machine())

    binaries: List[str] = []
    if arch_binary is not None:
        binaries += [arch_binary]
    binaries += ["qemu", "qemu-kvm"]
    for binary in binaries:
        if shutil.which(binary) is not None:
            return binary

    die("Couldn't find QEMU/KVM binary")


def find_qemu_firmware() -> Tuple[Path, bool]:
    FIRMWARE_LOCATIONS = [
        # UEFI firmware blobs are found in a variety of locations,
        # depending on distribution and package.
        *{
            "x86_64": ["/usr/share/ovmf/x64/OVMF_CODE.secboot.fd"],
            "i386": ["/usr/share/edk2/ovmf-ia32/OVMF_CODE.secboot.fd"],
        }.get(platform.machine(), []),
        "/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd",
        "/usr/share/qemu/OVMF_CODE.secboot.fd",
        "/usr/share/ovmf/OVMF.secboot.fd",
    ]

    for firmware in FIRMWARE_LOCATIONS:
        if os.path.exists(firmware):
            return Path(firmware), True

    warn("Couldn't find OVMF firmware blob with secure boot support, "
         "falling back to OVMF firmware blobs without secure boot support.")

    FIRMWARE_LOCATIONS = [
        # First, we look in paths that contain the architecture –
        # if they exist, they’re almost certainly correct.
        *{
            "x86_64": [
                "/usr/share/ovmf/ovmf_code_x64.bin",
                "/usr/share/ovmf/x64/OVMF_CODE.fd",
                "/usr/share/qemu/ovmf-x86_64.bin",
            ],
            "i386": ["/usr/share/ovmf/ovmf_code_ia32.bin", "/usr/share/edk2/ovmf-ia32/OVMF_CODE.fd"],
        }.get(platform.machine(), []),
        # After that, we try some generic paths and hope that if they exist,
        # they’ll correspond to the current architecture, thanks to the package manager.
        "/usr/share/edk2/ovmf/OVMF_CODE.fd",
        "/usr/share/qemu/OVMF_CODE.fd",
        "/usr/share/ovmf/OVMF.fd",
    ]

    for firmware in FIRMWARE_LOCATIONS:
        if os.path.exists(firmware):
            return Path(firmware), False

    die("Couldn't find OVMF UEFI firmware blob.")


def find_ovmf_vars() -> Path:
    OVMF_VARS_LOCATIONS = []

    if platform.machine() == "x86_64":
        OVMF_VARS_LOCATIONS += ["/usr/share/ovmf/x64/OVMF_VARS.fd"]
    elif platform.machine() == "i386":
        OVMF_VARS_LOCATIONS += ["/usr/share/edk2/ovmf-ia32/OVMF_VARS.fd"]

    OVMF_VARS_LOCATIONS += ["/usr/share/edk2/ovmf/OVMF_VARS.fd",
                            "/usr/share/qemu/OVMF_VARS.fd",
                            "/usr/share/ovmf/OVMF_VARS.fd"]

    for location in OVMF_VARS_LOCATIONS:
        if os.path.exists(location):
            return Path(location)

    die("Couldn't find OVMF UEFI variables file.")


def run_qemu(args: CommandLineArguments) -> None:
    has_kvm = os.path.exists("/dev/kvm")
    accel = "kvm" if has_kvm else "tcg"

    firmware, fw_supports_sb = find_qemu_firmware()

    cmdline = [
        find_qemu_binary(),
        "-machine",
        f"type=q35,accel={accel},smm={'on' if fw_supports_sb else 'off'}",
        "-smp",
        args.qemu_smp,
        "-m",
        args.qemu_mem,
        "-object",
        "rng-random,filename=/dev/urandom,id=rng0",
        "-device",
        "virtio-rng-pci,rng=rng0,id=rng-device0",
    ]

    if has_kvm:
        cmdline += ["-cpu", "host"]

    if args.qemu_headless:
        # -nodefaults removes the default CDROM device which avoids an error message during boot
        # -serial mon:stdio adds back the serial device removed by -nodefaults.
        cmdline += ["-nographic", "-nodefaults", "-serial", "mon:stdio"]
        # Fix for https://github.com/systemd/mkosi/issues/559. QEMU gets stuck in a boot loop when using BIOS
        # if there's no vga device.

    if not args.qemu_headless or (args.qemu_headless and "bios" in args.boot_protocols):
        cmdline += ["-vga", "virtio"]

    if args.network_veth:
        if not ensure_networkd(args):
            # Fall back to usermode networking if the host doesn't have networkd (eg: Debian)
            cmdline += ["-nic", "user,model=virtio-net-pci"]
        else:
            # Use vt- prefix so we can take advantage of systemd-networkd's builtin network file for VMs.
            ifname = f"vt-{virt_name(args)}"
            # vt-<image-name> is the ifname on the host and is automatically picked up by systemd-networkd which
            # starts a DHCP server on that interface. This gives IP connectivity to the VM. By default, QEMU
            # itself tries to bring up the vt network interface which conflicts with systemd-networkd which is
            # trying to do the same. By specifiying script=no and downscript=no, We tell QEMU to not touch vt
            # after it is created.
            cmdline += ["-nic", f"tap,script=no,downscript=no,ifname={ifname},model=virtio-net-pci"]

    if "uefi" in args.boot_protocols:
        cmdline += ["-drive", f"if=pflash,format=raw,readonly=on,file={firmware}"]

    with contextlib.ExitStack() as stack:
        if fw_supports_sb:
            ovmf_vars = stack.enter_context(copy_file_temporary(src=find_ovmf_vars(), dir=tmp_dir()))
            cmdline += [
                "-global",
                "ICH9-LPC.disable_s3=1",
                "-global",
                "driver=cfi.pflash01,property=secure,value=on",
                "-drive",
                f"file={ovmf_vars.name},if=pflash,format=raw",
            ]

        if args.ephemeral:
            f = stack.enter_context(copy_image_temporary(src=args.output, dir=args.output.parent))
            fname = Path(f.name)
        else:
            fname = args.output

        # Debian images fail to boot with virtio-scsi, see: https://github.com/systemd/mkosi/issues/725
        if args.distribution == Distribution.debian:
            cmdline += [
                "-drive",
                f"if=virtio,id=hd,file={fname},format={'qcow2' if args.qcow2 else 'raw'}",
            ]
        else:
            cmdline += [
                "-drive",
                f"if=none,id=hd,file={fname},format={'qcow2' if args.qcow2 else 'raw'}",
                "-device",
                "virtio-scsi-pci,id=scsi",
                "-device",
                "scsi-hd,drive=hd,bootindex=1",
            ]

        cmdline += args.cmdline

        print_running_cmd(cmdline)

        with suppress_stacktrace():
            run(cmdline, stdout=sys.stdout, stderr=sys.stderr)


def interface_exists(dev: str) -> bool:
    return run(["ip", "link", "show", dev], stdout=DEVNULL, stderr=DEVNULL, check=False).returncode == 0


def find_address(args: CommandLineArguments) -> Tuple[str, str]:
    name = virt_name(args)
    timeout = float(args.ssh_timeout)

    while timeout >= 0:
        stime = time.time()
        try:
            if interface_exists(f"ve-{name}"):
                dev = f"ve-{name}"
            elif interface_exists(f"vt-{name}"):
                dev = f"vt-{name}"
            else:
                raise MkosiException("Container/VM interface not found")

            link = json.loads(run(["ip", "-j", "link", "show", "dev", dev], stdout=PIPE, text=True).stdout)[0]
            if link["operstate"] == "DOWN":
                raise MkosiException(
                    f"{dev} is not enabled. Make sure systemd-networkd is running so it can manage the interface."
                )

            # Trigger IPv6 neighbor discovery of which we can access the results via 'ip neighbor'. This allows us to
            # find out the link-local IPv6 address of the container/VM via which we can connect to it.
            run(["ping", "-c", "1", "-w", "15", f"ff02::1%{dev}"], stdout=DEVNULL)

            for _ in range(50):
                neighbors = json.loads(
                    run(["ip", "-j", "neighbor", "show", "dev", dev], stdout=PIPE, text=True).stdout
                )

                for neighbor in neighbors:
                    dst = cast(str, neighbor["dst"])
                    if dst.startswith("fe80"):
                        return dev, dst

                time.sleep(0.4)
        except MkosiException as e:
            if time.time() - stime > timeout:
                die(str(e))

        time.sleep(1)
        timeout -= time.time() - stime

    die("Container/VM address not found")


def run_ssh(args: CommandLineArguments) -> None:
    ssh_key = args.ssh_key or args.output_sshkey
    assert ssh_key is not None

    if not ssh_key.exists():
        die(
            f"SSH key not found at {ssh_key}. Are you running from the project's root directory "
            "and did you build with the --ssh option?"
        )

    dev, address = find_address(args)

    with suppress_stacktrace():
        run(
            [
                "ssh",
                "-i",
                ssh_key,
                # Silence known hosts file errors/warnings.
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "LogLevel ERROR",
                f"root@{address}%{dev}",
                *args.cmdline,
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )


def run_serve(args: CommandLineArguments) -> None:
    """Serve the output directory via a tiny embedded HTTP server"""

    port = 8081
    image = args.output.parent

    if args.output_dir is not None:
        os.chdir(args.output_dir)

    with http.server.HTTPServer(("", port), http.server.SimpleHTTPRequestHandler) as httpd:
        print(f"Serving HTTP on port {port}: http://localhost:{port}/{image}")
        httpd.serve_forever()


def generate_secure_boot_key(args: CommandLineArguments) -> NoReturn:
    """Generate secure boot keys using openssl"""
    args.secure_boot_key = args.secure_boot_key or Path("./mkosi.secure-boot.key")
    args.secure_boot_certificate = args.secure_boot_certificate or Path("./mkosi.secure-boot.crt")

    keylength = 2048
    expiration_date = datetime.date.today() + datetime.timedelta(int(args.secure_boot_valid_days))
    cn = expand_specifier(args.secure_boot_common_name)

    for f in (args.secure_boot_key, args.secure_boot_certificate):
        if f.exists() and not args.force:
            die(
                dedent(
                    f"""\
                    {f} already exists.
                    If you are sure you want to generate new secure boot keys
                    remove {args.secure_boot_key} and {args.secure_boot_certificate} first.
                    """
                )
            )

    MkosiPrinter.print_step(f"Generating secure boot keys rsa:{keylength} for CN {cn!r}.")
    MkosiPrinter.info(
        dedent(
            f"""
            The keys will expire in {args.secure_boot_valid_days} days ({expiration_date:%A %d. %B %Y}).
            Remember to roll them over to new ones before then.
            """
        )
    )

    cmd: List[str] = [
        "openssl",
        "req",
        "-new",
        "-x509",
        "-newkey",
        f"rsa:{keylength}",
        "-keyout",
        os.fspath(args.secure_boot_key),
        "-out",
        os.fspath(args.secure_boot_certificate),
        "-days",
        str(args.secure_boot_valid_days),
        "-subj",
        f"/CN={cn}/",
        "-nodes",
    ]

    os.execvp(cmd[0], cmd)


def bump_image_version(args: CommandLineArguments) -> None:
    """Write current image version plus one to mkosi.version"""

    if args.image_version is None or args.image_version == "":
        print("No version configured so far, starting with version 1.")
        new_version = "1"
    else:
        v = args.image_version.split(".")

        try:
            m = int(v[-1])
        except ValueError:
            new_version = args.image_version + ".2"
            print(
                f"Last component of current version is not a decimal integer, appending '.2', bumping '{args.image_version}' → '{new_version}'."
            )
        else:
            new_version = ".".join(v[:-1] + [str(m + 1)])
            print(f"Increasing last component of version by one, bumping '{args.image_version}' → '{new_version}'.")

    open("mkosi.version", "w").write(new_version + "\n")


def expand_paths(paths: List[str]) -> List[str]:
    if not paths:
        return []

    environ = os.environ.copy()
    # Add a fake SUDO_HOME variable to allow non-root users specify
    # paths in their home when using mkosi via sudo.
    sudo_user = os.getenv("SUDO_USER")
    if sudo_user and "SUDO_HOME" not in environ:
        environ["SUDO_HOME"] = os.path.expanduser(f"~{sudo_user}")

    # No os.path.expandvars because it treats unset variables as empty.
    expanded = []
    for path in paths:
        try:
            expanded += [string.Template(path).substitute(environ)]
        except KeyError:
            # Skip path if it uses a variable not defined.
            pass
    return expanded


def prepend_to_environ_path(paths: List[Path]) -> None:
    if not paths:
        return

    news = [os.fspath(path) for path in paths]
    olds = os.getenv("PATH", "").split(":")
    os.environ["PATH"] = ":".join(news + olds)


def expand_specifier(s: str) -> str:
    user = os.getenv("SUDO_USER") or os.getenv("USER")
    assert user is not None
    return s.replace("%u", user)


def needs_build(args: Union[argparse.Namespace, CommandLineArguments]) -> bool:
    return args.verb == "build" or (not args.output.exists() and args.verb in MKOSI_COMMANDS_NEED_BUILD)


def run_verb(raw: argparse.Namespace) -> None:
    args = load_args(raw)

    prepend_to_environ_path(args.extra_search_paths)

    if args.verb == "genkey":
        generate_secure_boot_key(args)

    if args.verb == "bump":
        bump_image_version(args)

    if args.verb in MKOSI_COMMANDS_SUDO:
        check_root()
        unlink_output(args)

    if args.verb == "build":
        check_output(args)

    if args.verb == "summary":
        print_summary(args)

    if needs_build(args):
        check_root()
        check_native(args)
        init_namespace(args)
        manifest = build_stuff(args)

        if args.auto_bump:
            bump_image_version(args)

        save_manifest(args, manifest)

        print_output_size(args)

    if args.verb in ("shell", "boot"):
        run_shell(args)

    if args.verb == "qemu":
        run_qemu(args)

    if args.verb == "ssh":
        run_ssh(args)

    if args.verb == "serve":
        run_serve(args)
