/* SPDX-License-Identifier: GPL-2.0 */
/*
 * intf.h — shared interface between the xytro_sched BPF program and the
 * userspace tools (loader, xytro-top, xytro-steer, trainer).
 *
 * The BPF side gets __u32/__s32 from vmlinux.h; the userspace side gets them
 * from <linux/types.h> (include it before this header).
 */
#ifndef __XYTRO_INTF_H
#define __XYTRO_INTF_H

/*
 * Feature indices for the scoring policy. Keep in sync with XYTRO_NR_FEATS.
 * Features are fixed point in [0, 1024]; score_raw = sum(weights[i]*feats[i]).
 */
enum {
	XYTRO_F_WAKEUP    = 0,	/* 1024 if this enqueue is a wakeup, else 0 */
	XYTRO_F_NICE      = 1,	/* ((140 - static_prio) * 1024 / 40): higher = more priority */
	XYTRO_F_KTHREAD   = 2,	/* 1024 if the task is a kernel thread */
	XYTRO_F_UTIL      = 3,	/* recent run-length EWMA, 0..1024 (CPU-hog signal) */
	XYTRO_F_WAKE_FREQ = 4,	/* wakeup-frequency EWMA, 0..1024 (interactivity) */
	XYTRO_F_RQDEPTH   = 5,	/* global DSQ queue depth at enqueue, 0..1024 (pressure) */
	XYTRO_F_BIAS      = 6,	/* 1024 (constant) */
	XYTRO_NR_FEATS    = 7,
};

/* Dispatch lane chosen by the policy at enqueue time. */
enum {
	XYTRO_LANE_NORMAL = 0,	/* global DSQ, base slice */
	XYTRO_LANE_FAST   = 1,	/* local DSQ, preempting, boosted slice */
};

/* Telemetry event kinds. */
enum {
	XYTRO_EVT_DECISION = 0,	/* a policy decision was made at enqueue */
	XYTRO_EVT_RUNNING  = 1,	/* task picked to run on a CPU */
	XYTRO_EVT_STOPPING = 2,	/* task stopped running on a CPU */
};

/* Kernel scx_enq_flags bit values (stable; mirrored here so we don't depend
 * on vmlinux.h enum availability). */
#define XYTRO_ENQ_WAKEUP  (1ULL << 0)
#define XYTRO_ENQ_HEAD    (1ULL << 1)
#define XYTRO_ENQ_TAIL    (1ULL << 2)
#define XYTRO_ENQ_PREEMPT (1ULL << 4)

/* PF_KTHREAD bit from include/linux/sched.h (kernel macro, not in BTF). */
#define XYTRO_PF_KTHREAD  0x00200000UL

/* Max active (foreground) tasks tracked in the xytro_active map. */
#define XYTRO_ACTIVE_MAX  16

/* Policy parameters — single element of the xytro_policy array map.
 * Hot-updatable at runtime by xytro-steer / the trainer.
 * score_raw = sum(weights[i] * feats[i]); fast lane iff score_raw >= threshold. */
struct xytro_policy {
	__s32 weights[XYTRO_NR_FEATS];
	__s32 interactive_threshold;
	__s32 base_slice_ns;	/* normal lane slice */
	__s32 fast_slice_mult;	/* fast slice = base * mult / 1000 */
	__u32 dry_run;		/* 1 = record decisions but force normal lane */
	__u32 reserved;
};

#define XYTRO_POLICY_DEFAULT_BASE_SLICE_NS 2000000	/* 2 ms */
#define XYTRO_POLICY_DEFAULT_FAST_MULT     2000		/* 2.0x */
#define XYTRO_POLICY_DEFAULT_THRESHOLD     220000
#define XYTRO_POLICY_DEFAULT_WEIGHTS       { 200, 150, -400, -100, 150, 0, 0 }

/*
 * policy.bin layout (little-endian, XYTRO_POLICY_BIN_SIZE bytes):
 *   weights[XYTRO_NR_FEATS]  s32
 *   interactive_threshold    s32
 *   base_slice_ns            s32
 *   fast_slice_mult          s32
 *   dry_run                  u32
 */
#define XYTRO_POLICY_BIN_SIZE (XYTRO_NR_FEATS * 4 + 4 * 4)

/* One telemetry event pushed into the xytro_events ring buffer. */
struct xytro_evt {
	__u64 ts;		/* bpf_ktime_get_ns() timestamp */
	__u32 pid;
	__u32 cpu;
	__u32 kind;		/* XYTRO_EVT_* */
	__u32 state;		/* task->__state */
	char comm[16];
	/* --- decision fields (valid for XYTRO_EVT_DECISION) --- */
	__s32 score;		/* raw score at decision time */
	__s32 slice_ns;		/* slice granted */
	__u8  lane;		/* XYTRO_LANE_* */
	__u8  is_protected;	/* forced normal lane (kthread/pid1/loader) */
	__u8  is_dry_run;	/* decision recorded but not applied */
	__u8  is_active;	/* task is in the active (foreground) set */
	__s32 feats[XYTRO_NR_FEATS];	/* feature vector (fixed point) */
	/* --- latency (valid for XYTRO_EVT_RUNNING) --- */
	__u64 latency_ns;	/* wakeup→run latency sample, 0 if n/a */
};

#endif /* __XYTRO_INTF_H */
