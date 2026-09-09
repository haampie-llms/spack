import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

S = os.path.dirname(os.path.abspath(__file__))
SPACK = os.environ.get("SPACK", os.path.join(os.path.dirname(S), "bin", "spack"))
RESULTS = os.path.join(S, "results", "handwritten")


def write_base_scope():
    """Config scope registering the synthetic ``errtest`` repo and a solver time cap."""
    base = os.path.join(S, "base")
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "repos.yaml"), "w") as f:
        f.write("repos:\n  errtest: " + os.path.join(S, "repo", "spack_repo", "errtest") + "\n")
    with open(os.path.join(base, "concretizer.yaml"), "w") as f:
        f.write("concretizer:\n  timeout: 120\n  error_on_timeout: true\n")


CASES = []


def case(name, spec=None, config=None, env=None, note=""):
    CASES.append(dict(name=name, spec=spec, config=config, env=env, note=note))


# ---- root-spec cases, synthetic repo
case("R01_condvar", "condvar@1.0+feat", note="variant 'feat' only exists when @2:")
case("R02_condvalue", "condvalue@1.0 mode=c", note="value 'c' only valid when @2:")
case("R03_conddep", "conddep ^leaf", note="leaf is a dep only when +withleaf (default off)")
case("R04_chain", "chain-a ^leaf@2", note="chain-a -> chain-b requires leaf@1")
case(
    "R05_diamond",
    "chain-c",
    note="chain-c needs leaf@2, chain-a->chain-b needs leaf@1 (no user input at fault)",
)
case("R06_twoconds", "twoconds+x+y", note="+x needs leaf@1, +y needs leaf@2")
case("R07_platdep", "platdep ^leaf", note="leaf is a dep only on platform=linux")
case("R08_reqdir", "reqdir@1+feat", note="requires('@2:', when='+feat', msg=...)")
case("R09_cycle", "cyc-a", note="dependency cycle cyc-a <-> cyc-b")
case("R10_virtdep", "virtdep ^mpich@3.4", note="virtdep needs mpi@4:, mpich@3.4 too old")
case(
    "R11_needsfortran", "needsfortran %fortran=apple-clang", note="apple-clang provides no fortran"
)
case("R12_depvariant", "depvariant ^leaf+shared", note="depvariant requires leaf~shared")
case(
    "R13_langc_fortran",
    "langc %fortran=gcc",
    note="edge attribute for virtual fortran, langc never depends on fortran",
)
case(
    "R14_deep", "deep-root", note="chain-a->chain-b->leaf@1 vs deep-mid(+newleaf default)->leaf@3"
)
case(
    "R15_versiondep",
    "versiondep ^leaf@3",
    note="versiondep@2 needs leaf@2: (ok!), @1 needs leaf@1; this should SUCCEED with @2",
)
case("R16_versiondep_pin", "versiondep@1 ^leaf@2", note="versiondep@1 needs leaf@1")
# ---- root-spec cases, real packages
case("R20_hdf5_java", "hdf5@1.8.19+java", note="java variant only when @1.10:")
case("R21_hdf5_libaec", "hdf5 ^libaec", note="szip virtual dep only when +szip (default off)")
case("R22_hdf5_api", "hdf5@1.12 api=v114", note="conflicts() with msg")
case("R23_hdf5_notdep", "hdf5~mpi ^mpich", note="mpi dep only when +mpi")
case("R24_target", "zlib-ng target=x86_64", note="host is aarch64/m4")
case("R25_os", "zlib-ng os=ubuntu22.04", note="host os is sequoia")
case("R26_platform", "zlib-ng platform=linux", note="host platform is darwin")
case("R27_gcc99", "zlib-ng %gcc@99", note="no gcc@99 exists")
case("R28_mpi99", "hdf5 ^mpi@99", note="no mpi provider at version 99")
case("R29_pynumpy_py37", "py-numpy ^python@3.7", note="py-numpy requires python@3.11: or so")
case("R30_hash", "zlib-ng/deadbeefdeadbeefdeadbeefdeadbeef", note="unknown hash")
case("R31_hdf5_cmake_old", "hdf5@1.14 ^cmake@3.12", note="hdf5@1.14 needs cmake@3.18:")
case(
    "R32_hdf5_fortran_clang",
    "hdf5+fortran %fortran=apple-clang",
    note="apple-clang has no fortran",
)
case("R33_deprecated", "openssl@1.1.1w", note="deprecated version, config:deprecated false")
case(
    "R34_intelmpi_darwin",
    "hdf5+mpi ^intel-oneapi-mpi",
    note="intel mpi not available on darwin (conflict?)",
)
case("R35_buildsys", "zlib-ng build_system=meson", note="invalid variant value")
case("R36_two_c_compilers", "zlib-ng %c=gcc %c=apple-clang", note="two providers for c")
case(
    "R37_gcc_no_c",
    "zlib-ng %c=gcc",
    note="external gcc only provides fortran; gcc buildable so may just build gcc (long!)",
)
# ---- packages.yaml cases
case(
    "C01_nonbuildable",
    "zlib-ng",
    config="packages:\n  zlib-ng:\n    buildable: false\n",
    note="buildable:false, no external",
)
case(
    "C02_mpi_nonbuildable",
    "hdf5+mpi",
    config="packages:\n  mpi:\n    buildable: false\n",
    note="virtual buildable:false, no external provider",
)
case(
    "C03_ext_too_old",
    "cmake@3.25:",
    config="packages:\n  cmake:\n    buildable: false\n    externals:\n    - spec: cmake@3.20.0\n      prefix: /usr\n",  # noqa: E501
    note="only external is too old",
)
case(
    "C04_require_conflict",
    "hdf5+mpi",
    config="packages:\n  hdf5:\n    require: '~mpi'\n",
    note="config requires ~mpi, root asks +mpi",
)
case(
    "C05_require_badversion",
    "hdf5",
    config="packages:\n  hdf5:\n    require: '@99'\n",
    note="config requires nonexistent version",
)
case(
    "C06_require_badvariant",
    "hdf5",
    config="packages:\n  hdf5:\n    require: '+nonexistent'\n",
    note="config requires nonexistent variant",
)
case(
    "C07_all_gcc99",
    "zlib-ng",
    config="packages:\n  all:\n    require: '%gcc@99'\n",
    note="all: require nonexistent compiler version",
)
case(
    "C08_all_target",
    "zlib-ng",
    config="packages:\n  all:\n    require: 'target=x86_64'\n",
    note="all: require foreign target",
)
case(
    "C09_all_mpich_vs_openmpi",
    "hdf5 ^openmpi",
    config="packages:\n  all:\n    require: '^mpich'\n",
    note="config forces mpich, root asks openmpi",
)
case(
    "C10_virtual_require",
    "hdf5 ^mpich",
    config="packages:\n  mpi:\n    require: 'openmpi'\n",
    note="virtual requirement openmpi vs root mpich",
)
case(
    "C11_one_of_when",
    "hdf5@1.14+mpi",
    config="packages:\n  hdf5:\n    require:\n    - one_of: ['@1.12', '@1.10']\n      when: '+mpi'\n",  # noqa: E501
    note="conditional one_of excludes requested version",
)
case(
    "C12_require_condvar",
    "hdf5@1.8.19",
    config="packages:\n  hdf5:\n    require: '+java'\n",
    note="config requires conditional variant on old version",
)
case(
    "C13_gcc_nonbuildable",
    "zlib-ng %c=gcc",
    config="packages:\n  gcc:\n    buildable: false\n",
    note="gcc external has no c compiler and can't be built",
)
case(
    "C14_bad_provider",
    "hdf5+mpi",
    config="packages:\n  all:\n    providers:\n      mpi: [nonexistent-mpi]\n",
    note="provider list names nonexistent package (pref, may succeed)",
)
case(
    "C15_zlibapi_require",
    "hdf5 ^zlib-ng",
    config="packages:\n  zlib-api:\n    require: 'zlib'\n",
    note="virtual require zlib vs root zlib-ng",
)
case(
    "C16_all_noshared",
    "zlib-ng",
    config="packages:\n  all:\n    require: '~shared'\n",
    note="all: variant that not every package has (probably succeeds)",
)
case(
    "C17_python_ext_old",
    "py-numpy",
    config="packages:\n  python:\n    buildable: false\n    externals:\n    - spec: python@3.7.0\n      prefix: /usr\n",  # noqa: E501
    note="only python external too old for py-numpy",
)
case(
    "C18_pref_ignored",
    "hdf5@1.8.19",
    config="packages:\n  hdf5:\n    variants: +java\n",
    note="preference on conditional variant; should succeed silently?",
)
case(
    "C19_ext_variant",
    "cmake+ncurses",
    config="packages:\n  cmake:\n    buildable: false\n    externals:\n    - spec: cmake@3.30.0~ncurses\n      prefix: /usr\n",  # noqa: E501
    note="external lacks requested variant",
)
case(
    "C20_require_when_bad",
    "hdf5~mpi",
    config="packages:\n  hdf5:\n    require:\n    - spec: '+mpi'\n      when: '@1.14'\n",
    note="conditional requirement +mpi vs root ~mpi",
)
case(
    "C21_all_require_version",
    "zlib-ng",
    config="packages:\n  all:\n    require: '@2.2'\n",
    note="all: require version that most packages lack",
)
case(
    "C22_leaf_nonbuildable_chain",
    "chain-a",
    config="packages:\n  leaf:\n    buildable: false\n    externals:\n    - spec: leaf@2.0\n      prefix: /usr\n",  # noqa: E501
    note="external leaf@2 but chain-b needs leaf@1",
)
case(
    "C23_timeout",
    "hdf5+mpi+fortran ^openmpi",
    config="concretizer:\n  timeout: 1\n  error_on_timeout: true\n",
    note="what a timeout looks like",
)
# ---- environment cases
case(
    "E01_unify_version",
    env="spack:\n  specs: [leaf@2, chain-b]\n  concretizer: {unify: true}\n",
    note="unify:true, roots need leaf@2 and leaf@1",
)
case(
    "E02_unify_mpi",
    env="spack:\n  specs: ['hdf5 ^mpich', 'netcdf-c ^openmpi']\n  concretizer: {unify: true}\n",
    note="unify:true, two mpi providers",
)
case(
    "E03_unify_ok_when_possible",
    env="spack:\n  specs: [leaf@2, chain-b]\n  concretizer: {unify: when_possible}\n",
    note="should succeed",
)
case(
    "E04_env_condvar",
    env="spack:\n  specs: [condvar@1.0+feat, leaf]\n  concretizer: {unify: true}\n",
    note="same as R01 but in env with another root",
)
case(
    "E05_env_require_conflict",
    env="spack:\n  specs: [hdf5+mpi]\n  packages:\n    hdf5:\n      require: '~mpi'\n",
    note="requirement inside spack.yaml",
)


def run(c):
    d = os.path.join(S, "cases", c["name"])
    os.makedirs(d, exist_ok=True)
    cmd = [SPACK, "-C", os.path.join(S, "base")]
    if c["config"]:
        key = c["config"].split(":")[0].strip()
        with open(os.path.join(d, key + ".yaml"), "w") as f:
            f.write(c["config"])
        cmd += ["-C", d]
    if c["env"]:
        ed = os.path.join(d, "env")
        os.makedirs(ed, exist_ok=True)
        with open(os.path.join(ed, "spack.yaml"), "w") as f:
            f.write(c["env"])
        cmd += ["-e", ed, "concretize", "-f"]
    else:
        cmd += ["spec", c["spec"]]
    t = time.time()
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=dict(os.environ, SPACK_COLOR="never"),
    )
    dt = time.time() - t
    out = p.stdout
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, c["name"] + ".txt"), "w") as f:
        f.write(
            "# "
            + " ".join(cmd[3:])
            + "\n# note: "
            + c["note"]
            + f"\n# exit={p.returncode} time={dt:.1f}s lines={out.count(chr(10))} chars={len(out)}\n"  # noqa: E501
            + out
        )
    return dict(
        name=c["name"],
        exit=p.returncode,
        time=round(dt, 1),
        lines=out.count("\n"),
        chars=len(out),
        note=c["note"],
    )


write_base_scope()
sel = sys.argv[1:]
todo = [c for c in CASES if not sel or any(c["name"].startswith(s) for s in sel)]
with ThreadPoolExecutor(max_workers=4) as ex:
    results = list(ex.map(run, todo))
with open(os.path.join(RESULTS, "summary.json"), "w") as f:
    json.dump(results, f, indent=1)
for r in results:
    print(f"{r['name']:28s} exit={r['exit']} {r['time']:6.1f}s {r['lines']:4d}L {r['chars']:6d}c")
