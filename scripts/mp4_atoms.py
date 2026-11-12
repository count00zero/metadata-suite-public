import struct, sys
f = open(sys.argv[1], 'rb'); f.seek(0, 2); size = f.tell(); pos = 0
while pos < size:
    f.seek(pos); h = f.read(16)
    if len(h) < 8: print(f"{pos}: {size-pos} trailing bytes"); break
    n, t = struct.unpack('>I4s', h[:8])
    if n == 1: n = struct.unpack('>Q', h[8:16])[0]
    if n == 0: n = size - pos
    print(f"{pos:>12}  {t.decode('latin1'):<6} {n:>12}")
    if n < 8: print("invalid size, stop"); break
    pos += n
