# SPDX-License-Identifier: GPL-2.0
# Xytro M1/M2 — scored sched_ext scheduler + steering + training
#
# Targets:
#   make          build loader + tools (vmlinux.h + BPF obj + skeleton + loader)
#   make top      build the xytro-top telemetry viewer
#   make steer    build the xytro-steer policy CLI
#   make vmlinux  regenerate bpf/vmlinux.h from the running kernel's BTF
#   make clean
#
# Run (as root):
#   sudo ./bpf/xytro_sched                    # load + print telemetry
#   sudo ./bpf/xytro_sched --no-drain         # load; tools read pinned maps
#   sudo ./tools/xytro-top                    # live aggregate view (other term)
#   sudo ./tools/xytro-top --json t.jsonl     # record a trace for training
#   sudo ./tools/xytro-steer get              # inspect the live policy
#   sudo ./tools/xytro-steer load train/policy.bin

CLANG   ?= clang
BPFTOOL ?= bpftool
CC      ?= gcc

SCX_INC := third_party/scx/scheds/include
BPF     := bpf

VMLINUX_H := $(BPF)/vmlinux.h
BPF_OBJ   := $(BPF)/xytro_sched.bpf.o
SKEL_H    := $(BPF)/xytro_sched.skel.h
LOADER    := $(BPF)/xytro_sched
TOP       := tools/xytro-top
STEER     := tools/xytro-steer

CFLAGS     ?= -g -O2 -Wall
BPF_CFLAGS := -g -O2 -target bpf -D__TARGET_ARCH_x86 -Wall

.PHONY: all top steer clean vmlinux status

all: $(LOADER) $(TOP) $(STEER)

status:
	chmod +x tools/xytro-status
	./tools/xytro-status

top: $(TOP)

steer: $(STEER)

$(VMLINUX_H):
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

$(BPF_OBJ): $(BPF)/xytro_sched.bpf.c $(BPF)/intf.h $(VMLINUX_H)
	$(CLANG) $(BPF_CFLAGS) -I $(SCX_INC) -I $(BPF) \
		-c $(BPF)/xytro_sched.bpf.c -o $@

$(SKEL_H): $(BPF_OBJ)
	$(BPFTOOL) gen skeleton $(BPF_OBJ) > $@

$(LOADER): $(BPF)/xytro_sched.c $(SKEL_H) $(BPF)/intf.h
	$(CC) $(CFLAGS) -I $(BPF) -I $(SCX_INC) \
		$(BPF)/xytro_sched.c -o $@ -lbpf -lelf -lz

$(TOP): tools/xytro-top.c $(BPF)/intf.h
	$(CC) $(CFLAGS) -I $(BPF) -I $(SCX_INC) tools/xytro-top.c -o $@ -lbpf -lelf -lz

$(STEER): tools/xytro-steer.c $(BPF)/intf.h
	$(CC) $(CFLAGS) -I $(BPF) -I $(SCX_INC) tools/xytro-steer.c -o $@ -lbpf -lelf -lz

vmlinux: $(VMLINUX_H)

clean:
	rm -f $(VMLINUX_H) $(BPF_OBJ) $(SKEL_H) $(LOADER) $(TOP) $(STEER)
