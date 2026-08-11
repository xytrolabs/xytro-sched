// SPDX-License-Identifier: GPL-2.0
/*
 * xytro_sched.bpf.c — Xytro AI CPU scheduler (M1: learnable scoring policy).
 *
 * Policy:
 *   - On enqueue, build a feature vector (fixed point, 0..1024) and compute
 *     score_raw = sum(weights[i] * feats[i]) with weights from the
 *     xytro_policy array map (hot-updatable by xytro-steer / the trainer).
 *   - score_raw >= interactive_threshold → fast lane (preempting, boosted
 *     slice); otherwise → normal lane (global DSQ, base slice).
 *   - Protected tasks (kernel threads, pid 1, the loader process) always get
 *     the normal lane + base slice so an aggressive policy can't starve them.
 *   - dry_run records every decision but forces the normal lane.
 *   - Decision/running/stopping events are emitted for audit + M2 training.
 */
#include <scx/common.bpf.h>
#include <scx/compat.bpf.h>
#include <scx/user_exit_info.bpf.h>
#include "intf.h"

char _license[] SEC("license") = "GPL";

UEI_DEFINE(uei);

/* Telemetry ring buffer. */
struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 1 << 17);
} xytro_events SEC(".maps");

/* Policy parameters (hot-updatable; see intf.h). */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, u32);
	__type(value, struct xytro_policy);
} xytro_policy SEC(".maps");

/* Active (foreground) task thread-group ids. Written by userspace
 * (xytro-steer active add <pid> / the future agent). */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, XYTRO_ACTIVE_MAX);
	__type(key, u32);
	__type(value, u32);
} xytro_active SEC(".maps");

/* Per-task state. */
struct task_ctx {
	u64 last_run_ns;	/* timestamp of the last running() call */
	u64 wakeup_ts;		/* when the task was last woken (0 if none) */
	u32 util_ewma;		/* EWMA of recent run length, 0..1024 */
	u32 wake_ewma;		/* EWMA of wakeup frequency, 0..1024 */
	s32 target_cpu;		/* cpu chosen by select_cpu for this wakeup */
	u32 target_idle;	/* 1 if target_cpu was idle */
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct task_ctx);
} task_ctx_stor SEC(".maps");

/* Loader pid — written by userspace before attach; the scheduler never
 * starves its own loader process. */
volatile u64 xytro_loader_pid;

static struct task_ctx *get_task_ctx(struct task_struct *p, bool create)
{
	u32 flags = create ? BPF_LOCAL_STORAGE_GET_F_CREATE : 0;

	return bpf_task_storage_get(&task_ctx_stor, p, 0, flags);
}

static bool is_protected(struct task_struct *p)
{
	if (p->flags & XYTRO_PF_KTHREAD)
		return true;
	if (p->pid == 1)
		return true;
	if (xytro_loader_pid && (u64)(s32)p->pid == xytro_loader_pid)
		return true;
	return false;
}

/* True if the task belongs to a thread group marked as active (foreground). */
static bool is_active_task(struct task_struct *p)
{
	u32 tgid = (u32)p->tgid;
	u32 i;

	bpf_for(i, 0, XYTRO_ACTIVE_MAX) {
		u32 *pid = bpf_map_lookup_elem(&xytro_active, &i);

		if (pid && *pid == tgid)
			return true;
	}
	return false;
}

/* Pick a CPU guaranteed to be in the task's affinity mask, for safe
 * SCX_DSQ_LOCAL_ON routing. The kernel REJECTS a LOCAL_ON insert when the
 * target CPU is not in the task's cpus_ptr (runtime error: "SCX_DSQ_LOCAL[_ON]
 * target CPU N not allowed"), which crash-loops the loader. Prefer @preferred
 * (e.g. select_cpu's target) only if it is affine; else fall back to the task's
 * current CPU (always affine); else -1 (caller falls back to the shared queue).
 */
static s32 pick_affine_cpu(struct task_struct *p, s32 preferred)
{
	if (preferred >= 0 && bpf_cpumask_test_cpu(preferred, p->cpus_ptr))
		return preferred;
	preferred = (s32)scx_bpf_task_cpu(p);
	if (preferred >= 0 && bpf_cpumask_test_cpu(preferred, p->cpus_ptr))
		return preferred;
	return -1;
}

static void fill_feats(struct task_struct *p, struct task_ctx *tctx,
		       u64 enq_flags, s32 feats[XYTRO_NR_FEATS])
{
	s32 nr;

	feats[XYTRO_F_WAKEUP]    = (enq_flags & XYTRO_ENQ_WAKEUP) ? 1024 : 0;
	feats[XYTRO_F_NICE]      = (140 - p->static_prio) * 1024 / 40;
	feats[XYTRO_F_KTHREAD]   = (p->flags & XYTRO_PF_KTHREAD) ? 1024 : 0;
	feats[XYTRO_F_UTIL]      = tctx ? (s32)tctx->util_ewma : 0;
	feats[XYTRO_F_WAKE_FREQ] = tctx ? (s32)tctx->wake_ewma : 0;
	nr = scx_bpf_dsq_nr_queued(SCX_DSQ_GLOBAL);
	feats[XYTRO_F_RQDEPTH]   = nr < 0 ? 0 : (nr > 1024 ? 1024 : nr);
	feats[XYTRO_F_BIAS]      = 1024;
}

/* score_raw = sum(weights[i] * feats[i]), clamped to s32. */
static s32 compute_score(const struct xytro_policy *pol,
			 const s32 feats[XYTRO_NR_FEATS])
{
	s64 score = 0;
	int i;

	bpf_for(i, 0, XYTRO_NR_FEATS) {
		score += (s64)pol->weights[i] * feats[i];
	}
	if (score > 2147483647LL)
		return 2147483647;
	if (score < -2147483647LL)
		return -2147483647;
	return (s32)score;
}

s32 BPF_STRUCT_OPS(xytro_select_cpu, struct task_struct *p, s32 prev_cpu,
		   u64 wake_flags)
{
	struct task_ctx *tctx;
	bool is_idle;
	s32 cpu;

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
	tctx = get_task_ctx(p, false);
	if (tctx) {
		tctx->target_cpu = cpu;
		tctx->target_idle = is_idle ? 1 : 0;
	}
	return cpu;
}

void BPF_STRUCT_OPS(xytro_enqueue, struct task_struct *p, u64 enq_flags)
{
	struct xytro_policy *pol;
	struct task_ctx *tctx;
	struct xytro_evt *evt;
	s32 feats[XYTRO_NR_FEATS];
	s32 score = 0, slice_ns = SCX_SLICE_DFL;
	s32 pcpu = -1;
	u32 key = 0;
	bool fast = false, prot = false, act = false;
	int i;

	tctx = get_task_ctx(p, true);
	if (!tctx) {
		scx_bpf_dsq_insert(p, SCX_DSQ_GLOBAL, SCX_SLICE_DFL, enq_flags);
		return;
	}

	if (enq_flags & XYTRO_ENQ_WAKEUP) {
		tctx->wakeup_ts = bpf_ktime_get_ns();
		tctx->wake_ewma = (tctx->wake_ewma * 7 + 1024) / 8;
	}

	pol = bpf_map_lookup_elem(&xytro_policy, &key);
	if (pol) {
		fill_feats(p, tctx, enq_flags, feats);

		score = compute_score(pol, feats);
		prot = is_protected(p);
		act = is_active_task(p);
		/* The active (foreground) app always gets the fast lane, unless it is
		 * protected. dry_run only gates the learned decision, not this direct
		 * user preference. */
		/* Wakeup-enqueued tasks are latency-critical by nature: give them the
		 * CFS-like fast lane (per-CPU dsq + head + preempt) so wake-to-run
		 * latency stays low. This is a hard rule (like active-boost); the
		 * learned policy governs the non-wakeup path (migrations/periodic)
		 * and the slice boost. dry_run still forces the pure baseline. */
		fast = !prot && (act ||
				 (!pol->dry_run &&
				  ((enq_flags & XYTRO_ENQ_WAKEUP) ||
				   score >= pol->interactive_threshold)));

		slice_ns = pol->base_slice_ns;
		if (fast)
			slice_ns = (s32)(((u64)pol->base_slice_ns *
					  (u64)pol->fast_slice_mult) / 1000);
		if (slice_ns <= 0)
			slice_ns = SCX_SLICE_DFL;
	}

	/* Audit / training trace event. */
	evt = bpf_ringbuf_reserve(&xytro_events, sizeof(*evt), 0);
	if (evt) {
		evt->ts = bpf_ktime_get_ns();
		evt->pid = p->pid;
		evt->cpu = bpf_get_smp_processor_id();
		evt->kind = XYTRO_EVT_DECISION;
		evt->state = p->__state;
		bpf_core_read_str(evt->comm, sizeof(evt->comm), p->comm);
		evt->score = score;
		evt->slice_ns = slice_ns;
		evt->lane = fast ? XYTRO_LANE_FAST : XYTRO_LANE_NORMAL;
		evt->is_protected = prot;
		evt->is_dry_run = pol ? pol->dry_run : 0;
		evt->is_active = act;
		evt->latency_ns = 0;
		if (pol) {
			for (i = 0; i < XYTRO_NR_FEATS; i++)
				evt->feats[i] = feats[i];
		}
		bpf_ringbuf_submit(evt, 0);
	}

	if (fast) {
		/* Route the fast lane to a CPU that is guaranteed in the task's
		 * affinity mask (select_cpu's target if affine, else the task's own
		 * CPU), via the kernel's native per-CPU "local-on" dispatch queue
		 * (SCX_DSQ_LOCAL_ON | cpu), and kick that CPU. The kernel dispatches a
		 * local-on queue natively, so a wakeup runs immediately on its CPU - it
		 * never waits on the lazy default GLOBAL drain and never lands on a
		 * wrong CPU's LOCAL queue (the earlier stranding bug). pick_affine_cpu()
		 * guarantees the CPU is allowed, so we never hit the "target CPU not
		 * allowed" runtime error. */
		pcpu = (tctx && tctx->target_cpu >= 0)
			? pick_affine_cpu(p, tctx->target_cpu)
			: pick_affine_cpu(p, -1);
		if (pcpu >= 0) {
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (u32)pcpu,
					   (u64)slice_ns,
					   enq_flags | XYTRO_ENQ_HEAD | XYTRO_ENQ_PREEMPT);
			if (!tctx->target_idle)
				scx_bpf_kick_cpu(pcpu, SCX_KICK_PREEMPT);
		} else {
			scx_bpf_dsq_insert(p, SCX_DSQ_GLOBAL, (u64)slice_ns, enq_flags);
		}
	} else if (prot) {
		/* Protected tasks (kernel threads like kworkers, pid 1, the loader)
		 * are often CPU-affine-bound (e.g. kworker/12 is pinned to CPU 12).
		 * Route to their OWN CPU's native local-on queue (via pick_affine_cpu,
		 * which uses the task's current CPU - always affine) so it runs on a
		 * CPU it is allowed on. This avoids BOTH the stranding of SCX_DSQ_LOCAL
		 * (the enqueueing cpu may not be affine) and the "target CPU not
		 * allowed" runtime error of an unguarded LOCAL_ON. */
		pcpu = pick_affine_cpu(p, -1);
		if (pcpu >= 0)
			scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL_ON | (u32)pcpu,
					   (u64)slice_ns, enq_flags);
		else
			scx_bpf_dsq_insert(p, SCX_DSQ_GLOBAL, (u64)slice_ns, enq_flags);
	}
	else
		/* Slow lane: shared GLOBAL queue at TAIL (no preempt). A task here
		 * can be run by ANY CPU, so it can never be stranded by being bound
		 * to a busy (or non-affine) enqueue-CPU's local DSQ - the stranding
		 * that tripped the watchdog (runnable task stall for 18+s). The
		 * kernel's default dispatch drains GLOBAL whenever a CPU's local
		 * queue empties; the fast-lane depth guard + threshold floor keep
		 * the fast lane bounded so local queues empty regularly. */
		scx_bpf_dsq_insert(p, SCX_DSQ_GLOBAL, (u64)slice_ns, enq_flags);
}

void BPF_STRUCT_OPS(xytro_running, struct task_struct *p)
{
	struct task_ctx *tctx;
	struct xytro_evt *evt;
	u64 now, latency = 0;

	now = bpf_ktime_get_ns();
	tctx = get_task_ctx(p, true);
	if (!tctx)
		return;

	if (tctx->wakeup_ts) {
		latency = now - tctx->wakeup_ts;
		tctx->wakeup_ts = 0;
	}
	tctx->last_run_ns = now;

	evt = bpf_ringbuf_reserve(&xytro_events, sizeof(*evt), 0);
	if (!evt)
		return;
	evt->ts = now;
	evt->pid = p->pid;
	evt->cpu = bpf_get_smp_processor_id();
	evt->kind = XYTRO_EVT_RUNNING;
	evt->state = p->__state;
	bpf_core_read_str(evt->comm, sizeof(evt->comm), p->comm);
	evt->latency_ns = latency;
	bpf_ringbuf_submit(evt, 0);
}

void BPF_STRUCT_OPS(xytro_stopping, struct task_struct *p, bool runnable)
{
	struct task_ctx *tctx;
	struct xytro_evt *evt;
	u64 now, dt_us;
	u32 util;

	now = bpf_ktime_get_ns();
	tctx = get_task_ctx(p, false);
	if (!tctx || !tctx->last_run_ns)
		return;

	dt_us = (now - tctx->last_run_ns) / 1000;
	util = dt_us > 1024 ? 1024 : (u32)dt_us;
	tctx->util_ewma = (tctx->util_ewma * 15 + util) / 16;
	tctx->wake_ewma = (tctx->wake_ewma * 31) / 32;
	tctx->last_run_ns = 0;

	evt = bpf_ringbuf_reserve(&xytro_events, sizeof(*evt), 0);
	if (!evt)
		return;
	evt->ts = now;
	evt->pid = p->pid;
	evt->cpu = bpf_get_smp_processor_id();
	evt->kind = XYTRO_EVT_STOPPING;
	evt->state = p->__state;
	bpf_core_read_str(evt->comm, sizeof(evt->comm), p->comm);
	evt->latency_ns = 0;
	bpf_ringbuf_submit(evt, 0);
}

s32 BPF_STRUCT_OPS_SLEEPABLE(xytro_init)
{
	return 0;
}

void BPF_STRUCT_OPS(xytro_exit, struct scx_exit_info *info)
{
	UEI_RECORD(uei, info);
}

SCX_OPS_DEFINE(xytro_ops,
	       .select_cpu		= (void *)xytro_select_cpu,
	       .enqueue			= (void *)xytro_enqueue,
	       .running			= (void *)xytro_running,
	       .stopping		= (void *)xytro_stopping,
	       .init			= (void *)xytro_init,
	       .exit			= (void *)xytro_exit,
	       .timeout_ms		= 15000U,
	       .name			= "xytro_sched");
