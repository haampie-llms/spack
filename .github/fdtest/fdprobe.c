/* Report every open fd at startup: flags, type and (where possible) path, then lsof it. */
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *kind(mode_t m) {
    if (S_ISREG(m)) return "file";
    if (S_ISDIR(m)) return "dir";
    if (S_ISCHR(m)) return "chr";
    if (S_ISFIFO(m)) return "fifo/pipe";
    if (S_ISSOCK(m)) return "socket";
    if (S_ISLNK(m)) return "link";
    return "other";
}

int main(int argc, char **argv) {
    if (argc > 2 && strcmp(argv[1], "daemon") == 0) {
        /* daemonize like sccache/bazel: detach stdio, keep all other inherited fds */
        if (fork() != 0) return 0;
        setsid();
        int dn = open("/dev/null", O_RDWR);
        dup2(dn, 0); dup2(dn, 1); dup2(dn, 2);
        if (dn > 2) close(dn);
        sleep(atoi(argv[2]));
        return 0;
    }
    const char *label = argc > 1 ? argv[1] : "fdprobe";
    const char *out_path = getenv("FDPROBE_OUT");
    FILE *out = out_path ? fopen(out_path, "a") : NULL;
    if (!out) out = stderr;
    /* the fd of `out` itself is opened after the scan below would see it, so scan first */
    char buf[1 << 16];
    size_t len = 0;
    long max = sysconf(_SC_OPEN_MAX);
    if (max < 0 || max > 65536) max = 65536;
    len += snprintf(buf + len, sizeof buf - len, "=== %s pid=%d ppid=%d MAKEFLAGS='%s'\n", label, getpid(), getppid(), getenv("MAKEFLAGS") ? getenv("MAKEFLAGS") : "");
    for (int fd = 0; fd < max; fd++) {
        int fdfl = fcntl(fd, F_GETFD);
        if (fdfl == -1) continue;
        if (out != stderr && fd == fileno(out)) continue;
        int fl = fcntl(fd, F_GETFL);
        struct stat st;
        fstat(fd, &st);
        char path[PATH_MAX] = "";
#ifdef F_GETPATH
        if (fcntl(fd, F_GETPATH, path) == -1) path[0] = 0;
#else
        char lnk[64]; snprintf(lnk, sizeof lnk, "/proc/self/fd/%d", fd);
        ssize_t n = readlink(lnk, path, sizeof path - 1); path[n > 0 ? n : 0] = 0;
#endif
        const char *acc = (fl & O_ACCMODE) == O_RDONLY ? "r" : (fl & O_ACCMODE) == O_WRONLY ? "w" : "rw";
        len += snprintf(buf + len, sizeof buf - len, "  fd=%-4d %-9s %-2s ino=%-10llu %s %s\n", fd,
                        kind(st.st_mode), acc, (unsigned long long)st.st_ino,
                        (fdfl & FD_CLOEXEC) ? "CLOEXEC" : "inherit", path);
    }
    fputs(buf, out);
    fflush(out);
    if (getenv("FDPROBE_LSOF")) {
        char pid[32]; snprintf(pid, sizeof pid, "%d", getpid());
        pid_t c = fork();
        if (c == 0) {
            int o = fileno(out);
            dup2(o, 1);
            execlp("lsof", "lsof", "-n", "-P", "-a", "-p", pid, "-d", "0-65535", (char *)NULL);
            _exit(127);
        }
        waitpid(c, NULL, 0);
    }
    return 0;
}
