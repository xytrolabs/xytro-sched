// SPDX-License-Identifier: GPL-2.0
/*
 * xytro-top — live telemetry viewer / trace recorder for xytro_sched.
 *
 * Reads events from the pinned xytro_events ring buffer (while the loader is
 * running with --no-drain).
 *
 * Usage (as root):
 *   sudo ./tools/xytro-top                  aggregate summary (default)
 *   sudo ./tools/xytro-top --raw            print every event
 *   sudo ./tools/xytro-top --json out.jsonl write a JSONL trace (training)
 */
#include <errno.h>
#include <linux/types.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <scx/common.h>
#include "../bpf/intf.h"

#define EVENTS_PIN_PATH "/sys/fs/bpf/xytro_events"
#define PID_TRACK 512
#define TOP_SHOW 6

struct pidcnt {
	u32 pid;
	u64 n;
	char comm[16];
};

struct agg {
	time_t t0;		/* current 1s window start */
	u64 n_dec, n_fast, n_norm, n_run, n_stop;
	u64 lat_cnt;
	u64 lat_buckets[64];	/* log2 bucket of latency (ns) */
	struct pidcnt pids[PID_TRACK];
	u32 n_pids;
};

static volatile sig_atomic_t stop_req;
static time_t t_start;		/* cumulative start (for [T+Ns] display) */

static void on_signal(int sig)
{
	stop_req = 1;
}

static void pid_add(struct agg *a, u32 pid, const char *comm)
{
	u32 i;

	for (i = 0; i < a->n_pids; i++) {
		if (a->pids[i].pid == pid) {
			a->pids[i].n++;
			return;
		}
	}
	if (a->n_pids < PID_TRACK) {
		struct pidcnt *p = &a->pids[a->n_pids++];

		p->pid = pid;
		p->n = 1;
		snprintf(p->comm, sizeof(p->comm), "%s", comm);
	}
}

static void agg_reset(struct agg *a)
{
	memset(a, 0, sizeof(*a));
	a->t0 = time(NULL);
}

static u64 lat_pct(const struct agg *a, int pct)
{
	u64 total = a->lat_cnt, acc = 0;
	int b;

	if (!total)
		return 0;
	for (b = 0; b < 64; b++) {
		acc += a->lat_buckets[b];
		if (acc * 100 >= (u64)pct * total)
			return 1ULL << b;
	}
	return 1ULL << 63;
}

static void print_summary(const struct agg *a)
{
	u64 p50 = lat_pct(a, 50), p99 = lat_pct(a, 99);
	int shown = 0;
	u32 i;

	printf("[T+%lus] dec=%llu fast=%llu norm=%llu run=%llu stop=%llu "
	       "lat50=%lluus p99=%lluus | top: ",
	       (unsigned long)(time(NULL) - t_start),
	       (unsigned long long)a->n_dec, (unsigned long long)a->n_fast,
	       (unsigned long long)a->n_norm, (unsigned long long)a->n_run,
	       (unsigned long long)a->n_stop,
	       (unsigned long long)(p50 / 1000),
	       (unsigned long long)(p99 / 1000));

	for (i = 0; i < a->n_pids && shown < TOP_SHOW; i++) {
		if (!a->pids[i].n)
			continue;
		printf("%u(%s)=%llu ", a->pids[i].pid, a->pids[i].comm,
		       (unsigned long long)a->pids[i].n);
		shown++;
	}
	printf("\n");
}

static int handle_event_agg(void *ctx, void *data, size_t data_sz)
{
	const struct xytro_evt *evt = data;
	struct agg *a = ctx;
	u64 v;

	pid_add(a, evt->pid, evt->comm);
	switch (evt->kind) {
	case XYTRO_EVT_DECISION:
		a->n_dec++;
		if (evt->lane == XYTRO_LANE_FAST)
			a->n_fast++;
		else
			a->n_norm++;
		break;
	case XYTRO_EVT_RUNNING:
		a->n_run++;
		if (evt->latency_ns) {
			u32 b = 0;
			v = evt->latency_ns;
			while (v >>= 1)
				b++;
			if (b > 63)
				b = 63;
			a->lat_buckets[b]++;
			a->lat_cnt++;
		}
		break;
	case XYTRO_EVT_STOPPING:
		a->n_stop++;
		break;
	}
	return 0;
}

static int handle_event_raw(void *ctx, void *data, size_t data_sz)
{
	const struct xytro_evt *evt = data;

	if (evt->kind == XYTRO_EVT_DECISION)
		printf("cpu=%2u pid=%-7u dec score=%d slice=%d lane=%s%s%s %s\n",
		       evt->cpu, evt->pid, evt->score, evt->slice_ns,
		       evt->lane == XYTRO_LANE_FAST ? "fast" : "norm",
		       evt->is_protected ? " PROT" : "",
		       evt->is_dry_run ? " DRY" : "", evt->comm);
	else if (evt->kind == XYTRO_EVT_RUNNING)
		printf("cpu=%2u pid=%-7u run lat=%llu %s\n", evt->cpu, evt->pid,
		       (unsigned long long)evt->latency_ns, evt->comm);
	else
		printf("cpu=%2u pid=%-7u stop %s\n", evt->cpu, evt->pid, evt->comm);
	return 0;
}

struct json_ctx {
	FILE *out;
	u64 n;		/* event counter (for sampling) */
	u32 sample;	/* write 1 in `sample` events */
};

static int handle_event_json(void *ctx, void *data, size_t data_sz)
{
	struct json_ctx *jc = ctx;
	const struct xytro_evt *evt = data;
	FILE *out = jc->out;

	if (jc->sample > 1) {
		jc->n++;
		if (jc->n % jc->sample != 0)
			return 0;
	}

	if (evt->kind == XYTRO_EVT_DECISION) {
		char feats[128] = "";
		int i;

		for (i = 0; i < XYTRO_NR_FEATS; i++) {
			char tmp[32];
			int l = snprintf(tmp, sizeof(tmp), "%s%d",
					 i ? "," : "", evt->feats[i]);
			if (strlen(feats) + (size_t)l >= sizeof(feats))
				break;
			strcat(feats, tmp);
		}
		fprintf(out,
			"{\"ts\":%llu,\"pid\":%u,\"cpu\":%u,\"kind\":\"decision\","
			"\"comm\":\"%s\",\"score\":%d,\"slice\":%d,\"lane\":%d,"
			"\"prot\":%u,\"dry\":%u,\"feats\":[%s]}\n",
			(unsigned long long)evt->ts, evt->pid, evt->cpu,
			evt->comm, evt->score, evt->slice_ns, evt->lane,
			evt->is_protected, evt->is_dry_run, feats);
	} else
		fprintf(out,
			"{\"ts\":%llu,\"pid\":%u,\"cpu\":%u,\"kind\":\"%s\","
			"\"comm\":\"%s\",\"latency_ns\":%llu}\n",
			(unsigned long long)evt->ts, evt->pid, evt->cpu,
			evt->kind == XYTRO_EVT_RUNNING ? "running" : "stopping",
			evt->comm, (unsigned long long)evt->latency_ns);
	fflush(out);
	return 0;
}

int main(int argc, char **argv)
{
	enum { MODE_AGG, MODE_RAW, MODE_JSON } mode = MODE_AGG;
	FILE *json_out = NULL;
	struct json_ctx *jc = NULL;
	struct agg agg;
	u32 sample = 1;
	int map_fd;
	struct ring_buffer *rb;
	int err;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--raw")) {
			mode = MODE_RAW;
		} else if (!strcmp(argv[i], "--json") && i + 1 < argc) {
			mode = MODE_JSON;
			json_out = fopen(argv[++i], "w");
			if (!json_out) {
				fprintf(stderr, "cannot open %s: %s\n", argv[i],
					strerror(errno));
				return 1;
			}
		} else if (!strcmp(argv[i], "--sample") && i + 1 < argc) {
			sample = (u32)strtoul(argv[++i], NULL, 10);
			if (sample == 0)
				sample = 1;
		} else {
			fprintf(stderr,
				"usage: %s [--raw] [--json FILE] [--sample N]\n",
				argv[0]);
			return 2;
		}
	}

	map_fd = bpf_obj_get(EVENTS_PIN_PATH);
	if (map_fd < 0) {
		fprintf(stderr, "cannot open %s: %s\n",
			EVENTS_PIN_PATH, strerror(errno));
		fprintf(stderr, "is xytro_sched running with --no-drain?\n");
		return 1;
	}

	if (mode == MODE_AGG) {
		agg_reset(&agg);
		t_start = agg.t0;
		rb = ring_buffer__new(map_fd, handle_event_agg, &agg, NULL);
	} else if (mode == MODE_RAW) {
		rb = ring_buffer__new(map_fd, handle_event_raw, NULL, NULL);
	} else {
		jc = calloc(1, sizeof(*jc));
		if (!jc) {
			fprintf(stderr, "out of memory\n");
			close(map_fd);
			return 1;
		}
		jc->out = json_out;
		jc->sample = sample;
		rb = ring_buffer__new(map_fd, handle_event_json, jc, NULL);
	}
	if (!rb) {
		fprintf(stderr, "failed to create ring buffer consumer\n");
		close(map_fd);
		return 1;
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	while (!stop_req) {
		err = ring_buffer__poll(rb, 200);
		if (err < 0 && err != -EINTR) {
			fprintf(stderr, "ring buffer poll failed: %d\n", err);
			break;
		}
		if (mode == MODE_AGG && time(NULL) - agg.t0 >= 1) {
			print_summary(&agg);
			agg_reset(&agg);
		}
	}

	ring_buffer__free(rb);
	close(map_fd);
	if (json_out)
		fclose(json_out);
	return 0;
}
