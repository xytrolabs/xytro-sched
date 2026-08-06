// SPDX-License-Identifier: GPL-2.0
/*
 * xytro-steer — read/write the xytro_sched policy map at runtime (hot update,
 * no scheduler reload).
 *
 * Usage (as root, while xytro_sched is running):
 *   xytro-steer get
 *   xytro-steer set <feat-index 0..4> <value>
 *   xytro-steer threshold <value>
 *   xytro-steer slice <base_ns> <fast_mult>
 *   xytro-steer dry-run <0|1>
 *   xytro-steer reset
 *   xytro-steer dump <file.bin>
 *   xytro-steer load <file.bin>
 */
#include <errno.h>
#include <linux/types.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <scx/common.h>
#include "../bpf/intf.h"

#define POLICY_PIN_PATH "/sys/fs/bpf/xytro_policy"
#define ACTIVE_PIN_PATH "/sys/fs/bpf/xytro_active"

static int open_policy(void)
{
	int fd = bpf_obj_get(POLICY_PIN_PATH);

	if (fd < 0)
		fprintf(stderr, "cannot open %s: %s (is xytro_sched running?)\n",
			POLICY_PIN_PATH, strerror(errno));
	return fd;
}

static int get_policy(int fd, struct xytro_policy *pol)
{
	__u32 key = 0;

	return bpf_map_lookup_elem(fd, &key, pol);
}

static int put_policy(int fd, const struct xytro_policy *pol)
{
	__u32 key = 0;

	return bpf_map_update_elem(fd, &key, pol, BPF_ANY);
}

static int open_active(void)
{
	int fd = bpf_obj_get(ACTIVE_PIN_PATH);

	if (fd < 0)
		fprintf(stderr, "cannot open %s: %s (is xytro_sched running?)\n",
			ACTIVE_PIN_PATH, strerror(errno));
	return fd;
}

static int active_add(int fd, u32 pid)
{
	u32 i, key, val;

	for (i = 0; i < XYTRO_ACTIVE_MAX; i++) {
		key = i;
		if (bpf_map_lookup_elem(fd, &key, &val))
			return -1;
		if (val == 0)
			return bpf_map_update_elem(fd, &key, &pid, BPF_ANY);
	}
	fprintf(stderr, "active set is full (%u)\n", XYTRO_ACTIVE_MAX);
	return -1;
}

static int active_clear(int fd)
{
	u32 i, key, zero = 0;

	for (i = 0; i < XYTRO_ACTIVE_MAX; i++) {
		key = i;
		if (bpf_map_update_elem(fd, &key, &zero, BPF_ANY))
			return -1;
	}
	return 0;
}

static int active_list(int fd)
{
	u32 i, key, val;

	for (i = 0; i < XYTRO_ACTIVE_MAX; i++) {
		key = i;
		if (bpf_map_lookup_elem(fd, &key, &val))
			return -1;
		if (val)
			printf("  [%u] %u\n", i, val);
	}
	return 0;
}

static void print_policy(const struct xytro_policy *pol)
{
	static const char *names[XYTRO_NR_FEATS] = {
		"wakeup", "nice", "kthread", "util",
		"wake_freq", "rqdepth", "bias",
	};
	int i;

	printf("weights:\n");
	for (i = 0; i < XYTRO_NR_FEATS; i++)
		printf("  %-8s %d\n", names[i], pol->weights[i]);
	printf("interactive_threshold %d\n", pol->interactive_threshold);
	printf("base_slice_ns         %d\n", pol->base_slice_ns);
	printf("fast_slice_mult       %d\n", pol->fast_slice_mult);
	printf("dry_run               %u\n", pol->dry_run);
}

static int read_bin(const char *path, struct xytro_policy *pol)
{
	unsigned char buf[XYTRO_POLICY_BIN_SIZE];
	FILE *f;
	size_t n;

	f = fopen(path, "rb");
	if (!f) {
		fprintf(stderr, "cannot open %s: %s\n", path, strerror(errno));
		return -1;
	}
	n = fread(buf, 1, sizeof(buf), f);
	fclose(f);
	if (n != sizeof(buf)) {
		fprintf(stderr, "bad policy file %s (%zu bytes expected, got %zu)\n",
			path, sizeof(buf), n);
		return -1;
	}
	memcpy(pol->weights, buf, sizeof(pol->weights));
	memcpy(&pol->interactive_threshold, buf + 4 * XYTRO_NR_FEATS, 4);
	memcpy(&pol->base_slice_ns, buf + 4 * XYTRO_NR_FEATS + 4, 4);
	memcpy(&pol->fast_slice_mult, buf + 4 * XYTRO_NR_FEATS + 8, 4);
	memcpy(&pol->dry_run, buf + 4 * XYTRO_NR_FEATS + 12, 4);
	return 0;
}

static int write_bin(const char *path, const struct xytro_policy *pol)
{
	unsigned char buf[XYTRO_POLICY_BIN_SIZE];
	FILE *f;

	memcpy(buf, pol->weights, sizeof(pol->weights));
	memcpy(buf + 4 * XYTRO_NR_FEATS, &pol->interactive_threshold, 4);
	memcpy(buf + 4 * XYTRO_NR_FEATS + 4, &pol->base_slice_ns, 4);
	memcpy(buf + 4 * XYTRO_NR_FEATS + 8, &pol->fast_slice_mult, 4);
	memcpy(buf + 4 * XYTRO_NR_FEATS + 12, &pol->dry_run, 4);

	f = fopen(path, "wb");
	if (!f) {
		fprintf(stderr, "cannot open %s: %s\n", path, strerror(errno));
		return -1;
	}
	fwrite(buf, 1, sizeof(buf), f);
	fclose(f);
	return 0;
}

static void usage(const char *argv0)
{
	fprintf(stderr,
		"usage: %s <cmd> [args]\n"
		"  get\n"
		"  set <feat-index 0..%d> <value>\n"
		"  threshold <value>\n"
		"  slice <base_ns> <fast_mult>\n"
		"  dry-run <0|1>\n"
		"  reset\n"
		"  dump <file.bin>\n"
		"  load <file.bin>\n"
		"  active add <pid>\n"
		"  active clear\n"
		"  active list\n",
		argv0, XYTRO_NR_FEATS - 1);
}

int main(int argc, char **argv)
{
	struct xytro_policy pol;
	int fd, err;

	if (argc < 2) {
		usage(argv[0]);
		return 2;
	}

	fd = open_policy();
	if (fd < 0)
		return 1;

	err = get_policy(fd, &pol);
	if (err) {
		fprintf(stderr, "failed to read policy: %s\n", strerror(errno));
		close(fd);
		return 1;
	}

	if (!strcmp(argv[1], "get")) {
		print_policy(&pol);
	} else if (!strcmp(argv[1], "set") && argc == 4) {
		int idx = atoi(argv[2]);

		if (idx < 0 || idx >= XYTRO_NR_FEATS) {
			fprintf(stderr, "feat index must be 0..%d\n",
				XYTRO_NR_FEATS - 1);
			close(fd);
			return 2;
		}
		pol.weights[idx] = atoi(argv[3]);
		err = put_policy(fd, &pol);
	} else if (!strcmp(argv[1], "threshold") && argc == 3) {
		pol.interactive_threshold = atoi(argv[2]);
		err = put_policy(fd, &pol);
	} else if (!strcmp(argv[1], "slice") && argc == 4) {
		pol.base_slice_ns = atoi(argv[2]);
		pol.fast_slice_mult = atoi(argv[3]);
		err = put_policy(fd, &pol);
	} else if (!strcmp(argv[1], "dry-run") && argc == 3) {
		pol.dry_run = atoi(argv[2]) ? 1 : 0;
		err = put_policy(fd, &pol);
	} else if (!strcmp(argv[1], "reset")) {
		pol.weights[0] = 200;
		pol.weights[1] = 150;
		pol.weights[2] = -400;
		pol.weights[3] = -100;
		pol.weights[4] = 150;
		pol.weights[5] = 0;
		pol.weights[6] = 0;
		pol.interactive_threshold = XYTRO_POLICY_DEFAULT_THRESHOLD;
		pol.base_slice_ns = XYTRO_POLICY_DEFAULT_BASE_SLICE_NS;
		pol.fast_slice_mult = XYTRO_POLICY_DEFAULT_FAST_MULT;
		pol.dry_run = 0;
		err = put_policy(fd, &pol);
	} else if (!strcmp(argv[1], "dump") && argc == 3) {
		err = write_bin(argv[2], &pol);
	} else if (!strcmp(argv[1], "load") && argc == 3) {
		if (read_bin(argv[2], &pol)) {
			close(fd);
			return 1;
		}
		err = put_policy(fd, &pol);
		if (!err)
			printf("policy loaded from %s\n", argv[2]);
	} else if (!strcmp(argv[1], "active") && argc == 4 &&
		   !strcmp(argv[2], "add")) {
		int afd = open_active();

		if (afd < 0) {
			close(fd);
			return 1;
		}
		err = active_add(afd, (u32)atoi(argv[3]));
		close(afd);
	} else if (!strcmp(argv[1], "active") && argc == 3 &&
		   !strcmp(argv[2], "clear")) {
		int afd = open_active();

		if (afd < 0) {
			close(fd);
			return 1;
		}
		err = active_clear(afd);
		close(afd);
	} else if (!strcmp(argv[1], "active") && argc == 3 &&
		   !strcmp(argv[2], "list")) {
		int afd = open_active();

		if (afd < 0) {
			close(fd);
			return 1;
		}
		err = active_list(afd);
		close(afd);
	} else {
		usage(argv[0]);
		close(fd);
		return 2;
	}

	if (err) {
		fprintf(stderr, "failed: %s\n", strerror(errno));
		close(fd);
		return 1;
	}

	close(fd);
	return 0;
}
