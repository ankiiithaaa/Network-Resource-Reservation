#!/usr/bin/env bash
# Diagnostic only. Does not install packages or change host network settings.
set -u
printf '$ id\n'
id
printf '\n$ cat /etc/os-release\n'
cat /etc/os-release
printf '\n$ sudo apt update\n'
sudo apt update
printf '\n$ command -v mn ovs-vsctl iperf3\n'
command -v mn ovs-vsctl iperf3
printf '\n$ capsh --print\n'
capsh --print
printf '\n$ unshare -Urn ip link add p15test type veth peer name p15peer\n'
unshare -Urn ip link add p15test type veth peer name p15peer
