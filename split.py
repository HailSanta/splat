#! /usr/bin/python3

import argparse
import importlib
import importlib.util
import os
from ranges import Range, RangeDict
import re
from pathlib import Path
import segtypes
import sys
import yaml
import pickle
from colorama import Style, Fore
from collections import OrderedDict
from segtypes.segment import parse_segment_type
from segtypes.code import N64SegCode
from util import log

parser = argparse.ArgumentParser(
    description="Split a rom given a rom, a config, and output directory")
parser.add_argument("rom", help="path to a .z64 rom")
parser.add_argument("config", help="path to a compatible config .yaml file")
parser.add_argument("outdir", help="a directory in which to extract the rom")
parser.add_argument("--modes", nargs="+", default="all")
parser.add_argument("--verbose", action="store_true",
                    help="Enable debug logging")
parser.add_argument("--new", action="store_true",
                    help="Only split changed segments in config")


def write_ldscript(rom_name, repo_path, sections, bare=False):
    with open(os.path.join(repo_path, rom_name + ".ld"), "w", newline="\n") as f:
        if bare:
            f.write("\n".join(sections))
        else:
            f.write(
                "SECTIONS\n"
                "{\n"
                "    "
            )
            f.write("\n    ".join(s.replace("\n", "\n    ") for s in sections)[:-4])
            f.write(
                "}\n"
            )


def parse_file_start(split_file):
    return split_file[0] if "start" not in split_file else split_file["start"]


def gather_c_funcs(repo_path):
    funcs = {}
    special_labels = {}
    labels_to_add = set()
    ranges = RangeDict()

    # Manual list of func name / addrs
    func_addrs_path = os.path.join(repo_path, "tools", "symbol_addrs.txt")
    if os.path.exists(func_addrs_path):
        with open(func_addrs_path) as f:
            func_addrs_lines = f.readlines()

        for line in func_addrs_lines:
            line = line.strip()
            if not line == "" and not line.startswith("//"):
                comment_loc = line.find("//")
                line_ext = ""

                if comment_loc != -1:
                    line_ext = line[comment_loc + 2:].strip()
                    line = line[:comment_loc].strip()

                line_split = line.split("=")
                name = line_split[0].strip()
                addr = int(line_split[1].strip()[:-1], 0)
                funcs[addr] = name

                if line_ext:
                    for info in line_ext.split(" "):
                        if info == "!":
                            labels_to_add.add(name)
                            special_labels[addr] = name
                        if info.startswith("size:"):
                            size = int(info.split(":")[1], 0)
                            ranges.add(Range(addr, addr + size), name)

    return funcs, labels_to_add, special_labels, ranges


def gather_c_variables(repo_path):
    vars = {}

    undefined_syms_path = os.path.join(repo_path, "undefined_syms.txt")
    if os.path.exists(undefined_syms_path):
        with open(undefined_syms_path) as f:
            us_lines = f.readlines()

        for line in us_lines:
            line = line.strip()
            if not line == "" and not line.startswith("//"):
                line_split = line.split("=")
                name = line_split[0].strip()
                addr = int(line_split[1].strip()[:-1], 0)
                vars[addr] = name

    return vars


def get_base_segment_class(seg_type):
    try:
        segmodule = importlib.import_module("segtypes." + seg_type)
    except ModuleNotFoundError:
        return None

    return getattr(segmodule, "N64Seg" + seg_type[0].upper() + seg_type[1:])


def get_extension_dir(options, config_path):
    if "extensions" not in options:
        return None
    return os.path.join(Path(config_path).parent, options["extensions"])


def get_extension_class(options, config_path, seg_type):
    ext_dir = get_extension_dir(options, config_path)
    if ext_dir == None:
        return None

    try:
        ext_spec = importlib.util.spec_from_file_location(f"segtypes.{seg_type}", os.path.join(ext_dir, f"{seg_type}.py"))
        ext_mod = importlib.util.module_from_spec(ext_spec)
        ext_spec.loader.exec_module(ext_mod)
    except Exception as err:
        log.write(err, status="error")
        return None

    return getattr(ext_mod, "N64Seg" + seg_type[0].upper() + seg_type[1:])


def fmt_size(size):
    if size > 1000000:
        return str(size // 1000000) + " MB"
    elif size > 1000:
        return str(size // 1000) + " KB"
    else:
        return str(size) + " B"


def main(rom_path, config_path, repo_path, modes, verbose, ignore_cache=False):
    with open(rom_path, "rb") as f:
        rom_bytes = f.read()

    # Create main output dir
    Path(repo_path).mkdir(parents=True, exist_ok=True)

    # Load config
    with open(config_path) as f:
        config = yaml.safe_load(f.read())

    options = config.get("options")
    options["modes"] = modes
    options["verbose"] = verbose

    c_funcs, c_func_labels_to_add, special_labels, ranges = gather_c_funcs(repo_path)
    c_vars = gather_c_variables(repo_path)

    ran_segments = []
    ld_sections = []
    seen_segment_names = set()

    defined_funcs = set()
    undefined_funcs = set()
    undefined_syms = set()

    seg_sizes = {}
    seg_split = {}
    seg_cached = {}

    # Load cache
    cache_path = Path(repo_path) / ".splat_cache"
    try:
        with open(cache_path, "rb") as f:
            cache = pickle.load(f)
    except Exception:
        cache = {}

    # Initialize segments
    for i, segment in enumerate(config['segments']):
        if len(segment) == 1:
            # We're at the end
            continue

        seg_type = parse_segment_type(segment)

        segment_class = get_base_segment_class(seg_type)
        if segment_class == None:
            # Look in extensions
            segment_class = get_extension_class(options, config_path, seg_type)

        if segment_class == None:
            log.write(f"ERROR: could not load {seg_type} segment type. Confirm your extension directory is configured correctly", status="error")
            exit(1)

        segment = segment_class(segment, config['segments'][i + 1], options)

        if segment_class.require_unique_name:
            if segment.name in seen_segment_names:
                log.write(f"ERROR: Segment name {segment.name} is not unique", status="error")
                exit(1)
            seen_segment_names.add(segment.name)

        if type(segment) == N64SegCode:
            segment.all_functions = defined_funcs
            segment.c_functions = c_funcs
            segment.c_variables = c_vars
            segment.special_labels = special_labels
            segment.c_labels_to_add = c_func_labels_to_add
            segment.symbol_ranges = ranges

        segment.check()

        tp = segment.type
        if segment.type == "bin" and segment.is_name_default():
            tp = "unk"

        if tp not in seg_sizes:
            seg_sizes[tp] = 0
            seg_split[tp] = 0
            seg_cached[tp] = 0
        seg_sizes[tp] += segment.rom_length

        if segment.should_run():
            # Check cache
            cached = segment.cache()
            if not ignore_cache and cached == cache.get(segment.unique_id()):
                log.dot(status="skip")
                seg_cached[tp] += 1

                continue
            cache[segment.unique_id()] = cached

            if verbose:
                log.write(f"{Style.DIM}0x{segment.rom_start:06X}{Style.RESET_ALL} {segment.type} {Style.BRIGHT}{segment.name}{Style.RESET_ALL}{Style.DIM}...", end="")

            segment.split(rom_bytes, repo_path)
            ran_segments.append(segment)

            if type(segment) == N64SegCode:
                defined_funcs |= segment.glabels_added
                undefined_funcs |= segment.glabels_to_add
                undefined_syms |= segment.undefined_syms_to_add

            if verbose:
                log.write(f"ok", status="ok")
            else:
                log.dot(status="ok")

            seg_split[tp] += 1

        ld_sections.append(segment.get_ld_section())

    for segment in ran_segments:
        segment.postsplit(ran_segments)

    # Write ldscript
    if "ld" in options["modes"] or "all" in options["modes"]:
        if verbose:
            log.write("Writing linker script")
        write_ldscript(config['basename'], repo_path, ld_sections, options.get("ld_bare", False))

    # Write undefined_funcs_auto.txt
    c_predefined_funcs = set(c_funcs.keys())
    to_write = sorted(undefined_funcs - defined_funcs - c_predefined_funcs)
    if len(to_write) > 0:
        with open(os.path.join(repo_path, "undefined_funcs_auto.txt"), "w", newline="\n") as f:
            for line in to_write:
                f.write(line + " = 0x" + line.split("_")[1][:8].upper() + ";\n")

    # write undefined_syms_auto.txt
    to_write = sorted(undefined_syms)
    if len(to_write) > 0:
        with open(os.path.join(repo_path, "undefined_syms_auto.txt"), "w", newline="\n") as f:
            for sym in to_write:
                f.write(f"{sym} = 0x{sym[2:]};\n")

    # Statistics
    unk_size = seg_sizes.get("unk", 0)
    rest_size = 0
    total_size = len(rom_bytes)

    for tp in seg_sizes:
        if tp != "unk":
            rest_size += seg_sizes[tp]

    assert(unk_size + rest_size == total_size)

    known_ratio = rest_size / total_size
    unk_ratio = unk_size / total_size

    log.write(f"Split {fmt_size(rest_size)} ({known_ratio:.2%}) in defined segments")
    for tp in seg_sizes:
        if tp != "unk":
            tmp_size = seg_sizes[tp]
            tmp_ratio = tmp_size / total_size
            log.write(f"{tp:>20}: {fmt_size(tmp_size):>8} ({tmp_ratio:.2%}) {Fore.GREEN}{seg_split[tp]} split{Style.RESET_ALL}, {Style.DIM}{seg_cached[tp]} cached")
    log.write(f"{'unknown':>20}: {fmt_size(unk_size):>8} ({unk_ratio:.2%}) from unknown bin files")

    # Save cache
    if cache != {}:
        if verbose:
            print("Writing cache")
        with open(cache_path, "wb") as f:
            pickle.dump(cache, f)

if __name__ == "__main__":
    args = parser.parse_args()
    main(args.rom, args.config, args.outdir, args.modes, args.verbose, not args.new)
