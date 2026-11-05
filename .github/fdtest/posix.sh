#!/bin/bash
# usage: posix.sh pass|fail -- run from the spack root. With "fail", the fd checks must fail.
expect=$1
scope="-C .github/fdtest/scope"
tmp=${RUNNER_TEMP:-/tmp}
cc -O2 -Wall -o "$tmp/fdprobe" .github/fdtest/fdprobe.c || exit 2
export FDPROBE=$tmp/fdprobe
python3 -c "import multiprocessing as m, sys; print(sys.version, m.get_start_method())"
# bootstrap clingo with the default config: the test scope does not have the builtin repo
bin/spack bootstrap now || exit 2
fd_failures=0

for variant in "~gmake" "+gmake"; do
  echo "::group::fd-probe$variant"
  export FDPROBE_OUT=$tmp/fdprobe$variant.txt
  rm -f "$FDPROBE_OUT"
  bin/spack $scope uninstall -ya fd-probe > /dev/null 2>&1
  # fd 9 is inheritable: under fork it reaches the build process, and must not leak further
  bin/spack $scope install "fd-probe$variant" 9</dev/null || exit 2
  bin/spack $scope python .github/fdtest/check.py "$FDPROBE_OUT" || fd_failures=$((fd_failures + 1))
  echo "::endgroup::"
done

echo "::group::daemon keeps fds open"
export FDPROBE_DAEMON=${FDPROBE_DAEMON_SECS:-60}
bin/spack $scope uninstall -ya fd-probe > /dev/null 2>&1
start=$(date +%s)
bin/spack $scope install fd-probe 9</dev/null 2>&1 | cat
elapsed=$(( $(date +%s) - start ))
echo "stdout of spack reached EOF after ${elapsed}s, daemon lives ${FDPROBE_DAEMON}s"
[ "$elapsed" -lt $((FDPROBE_DAEMON / 2)) ] || fd_failures=$((fd_failures + 1))
echo "::endgroup::"

echo "fd check failures: $fd_failures (expected: $expect)"
if [ "$expect" = pass ]; then [ "$fd_failures" -eq 0 ]; else [ "$fd_failures" -gt 0 ]; fi
