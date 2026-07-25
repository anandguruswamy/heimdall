# UNO Q Runtime

This directory will contain the Linux-side Heimdall runtime. The intended
seams are:

```text
CDC adapter -> frame decoder -> canonical observation stream -> fusion/storage
capture replay -----------------------------------------------^
```

The runtime should remain usable with a capture file when no gateway is
connected.
