// SPDX-License-Identifier: GPL-2.0
/*
 * xytro_sched.c — userspace loader for the xytro_sched sched_ext scheduler.
 *
 * Loads and attaches the BPF scheduler, initializes the policy map (defaults
 * or a --policy file), drains telemetry, and detaches on SIGINT/SIGTERM — at
 * which point the kernel falls back to the default scheduler.
 *
 * Usage:
 *   sudo ./bpf/xytro_sched                     # load + print telemetry
 *   sudo ./bpf/xytro_sched --no-drain          # load; xytro-top drains
 *   sudo ./bpf/xytro_sched --policy train/policy.bin
 */
#include <errno.h>
#include <linux/types.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>
#include <scx/user_exit_info.h>
#include "xytro_sched.skel.h"
#include "intf.h"

#define EVENTS_PIN_PATH "/sys/fs/bpf/xytro_events"
#define POLICY_PIN_PATH "/sys/fs/bpf/xytro_policy"
#define ACTIVE_PIN_PATH "/sys/fs/bpf/xytro_active"

static volatile sig_atomic_t exit_req;

/* Default policy (see intf.h). */
static const struct xytro_policy default_policy = {
	.weights		= XYTRO_POLICY_DEFAULT_WEIGHTS,
	.interactive_threshold	= XYTRO_POLICY_DEFAULT_THRESHOLD,
	.base_slice_ns		= XYTRO_POLICY_DEFAULT_BASE_SLICE_NS,
	.fast_slice_mult	= XYTRO_POLICY_DEFAULT_FAST_MULT,
	.dry_run		= 0,
	.reserved		= 0,
};

static int libbpf_print_fn(enum libbpf_print_level level, const char *fmt,
			   va_list args)
{
	if (level == LIBBPF_DEBUG)
		return 0;
	return vfprintf(stderr, fmt, args);
}

static void on_signal(int sig)
{
	exit_req = 1;
}

/* Read a policy.bin file (little-endian; see XYTRO_POLICY_BIN_SIZE). */
static int read_policy_file(const char *path, struct xytro_policy *pol)
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

static void on_event(const struct xytro_evt *evt)
{
	if (evt->kind == XYTRO_EVT_DECISION)
		printf("cpu=%2u pid=%-7u dec score=%d slice=%d lane=%s%s%s %s\n",
		       evt->cpu, evt->pid, evt->score, evt->slice_ns,
		       evt->lane == XYTRO_LANE_FAST ? "fast" : "norm",
		       evt->is_protected ? " PROT" : "",
		       evt->is_dry_run ? " DRY" : "", evt->comm);
	else if (evt->kind == XYTRO_EVT_RUNNING)
		printf("cpu=%2u pid=%-7u run lat=%llu %s\n",
		       evt->cpu, evt->pid, (unsigned long long)evt->latency_ns,
		       evt->comm);
	else
		printf("cpu=%2u pid=%-7u stop %s\n", evt->cpu, evt->pid, evt->comm);
}

static int handle_event(void *ctx, void *data, size_t data_sz)
{
	on_event(data);
	return 0;
}

int main(int argc, char **argv)
{
	struct xytro_sched_bpf *skel;
	struct ring_buffer *rb = NULL;
	const char *policy_file = NULL;
	struct xytro_policy pol;
	bool no_drain = false;
	u32 key = 0;
	int err;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--no-drain"))
			no_drain = true;
		else if (!strcmp(argv[i], "--policy") && i + 1 < argc)
			policy_file = argv[++i];
		else {
			fprintf(stderr, "usage: %s [--no-drain] [--policy FILE.bin]\n",
				argv[0]);
			return 2;
		}
	}

	libbpf_set_print(libbpf_print_fn);

	skel = xytro_sched_bpf__open();
	if (!skel) {
		fprintf(stderr, "failed to open skeleton\n");
		return 1;
	}

	/* Populate the __SCX_* rodata enums (DSQ ids, slices, flags) from the
	 * running kernel's BTF before load. Without this they stay 0 and the
	 * scheduler dispatches to DSQ 0 → "non-existent DSQ 0x0". */
	SCX_ENUM_INIT(skel);

	/* Never let the scheduler starve its own loader process. */
	skel->bss->xytro_loader_pid = getpid();

	err = xytro_sched_bpf__load(skel);
	if (err) {
		fprintf(stderr, "failed to load: %d\n", err);
		goto out;
	}

	if (policy_file) {
		if (read_policy_file(policy_file, &pol)) {
			fprintf(stderr, "failed to read policy file\n");
			goto out;
		}
		printf("xytro_sched: applying policy from %s\n", policy_file);
	} else {
		pol = default_policy;
	}

	err = bpf_map_update_elem(bpf_map__fd(skel->maps.xytro_policy),
				   &key, &pol, BPF_ANY);
	if (err)
		fprintf(stderr, "warning: failed to set policy: %d\n", err);

	/* Expose the maps to xytro-top / xytro-steer via bpffs. Drop any stale
	 * pin from a previous run first so bpf_map__pin doesn't hit -EEXIST. */
	unlink(EVENTS_PIN_PATH);
	err = bpf_map__pin(skel->maps.xytro_events, EVENTS_PIN_PATH);
	if (err)
		fprintf(stderr, "warning: failed to pin events map: %d\n", err);
	unlink(POLICY_PIN_PATH);
	err = bpf_map__pin(skel->maps.xytro_policy, POLICY_PIN_PATH);
	if (err)
		fprintf(stderr, "warning: failed to pin policy map: %d\n", err);
	unlink(ACTIVE_PIN_PATH);
	err = bpf_map__pin(skel->maps.xytro_active, ACTIVE_PIN_PATH);
	if (err)
		fprintf(stderr, "warning: failed to pin active map: %d\n", err);

	err = xytro_sched_bpf__attach(skel);
	if (err) {
		fprintf(stderr, "failed to attach: %d\n", err);
		if (err == -EBUSY)
			fprintf(stderr,
				"another sched_ext scheduler is already running. "
				"Stop it first (Ctrl+C its loader, or check "
				"`scx_loader.service`) then retry.\n");
		goto out;
	}

	if (!no_drain) {
		rb = ring_buffer__new(bpf_map__fd(skel->maps.xytro_events),
				      handle_event, NULL, NULL);
		if (!rb) {
			fprintf(stderr, "failed to create ring buffer consumer\n");
			goto out;
		}
	}

	if (signal(SIGINT, on_signal) == SIG_ERR ||
	    signal(SIGTERM, on_signal) == SIG_ERR) {
		fprintf(stderr, "failed to install signal handlers\n");
		goto out;
	}

	printf("xytro_sched: attached (scheduler is live). Ctrl+C to detach; "
	       "the kernel then falls back to the default scheduler.\n");

	while (!exit_req && !UEI_EXITED(skel, uei)) {
		if (rb) {
			err = ring_buffer__poll(rb, 200);
			if (err < 0 && err != -EINTR) {
				fprintf(stderr, "ring buffer poll failed: %d\n", err);
				break;
			}
		} else {
			/* --no-drain: no local consumer; just stay alive so
			 * xytro-top / xytro-steer can use the pinned maps. */
			sleep(1);
		}
	}

	if (UEI_EXITED(skel, uei)) {
		fprintf(stderr, "xytro_sched: scheduler was disabled by the kernel:\n");
		UEI_REPORT(skel, uei);
	}
	printf("xytro_sched: detaching...\n");

out:
	ring_buffer__free(rb);
	bpf_map__unpin(skel->maps.xytro_events, EVENTS_PIN_PATH);
	bpf_map__unpin(skel->maps.xytro_policy, POLICY_PIN_PATH);
	bpf_map__unpin(skel->maps.xytro_active, ACTIVE_PIN_PATH);
	xytro_sched_bpf__destroy(skel);
	return 0;
}
