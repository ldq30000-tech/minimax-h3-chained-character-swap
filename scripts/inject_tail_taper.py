#!/usr/bin/env python3
"""Inject chroma noise into the tail N frames with a tapered alpha ramp.

Recipe: same as inject_tail.py (inject_variant.py 'full' mode), but the last
`ramp` frames of the injected tail taper linearly from `alpha` down to
`alpha_end`. Frames before the tail pass through untouched.

Example: tail=22, alpha=0.45, alpha_end=0.10, ramp=3
  -> frames tail[0..18] at 0.45; last 3 at ~0.33 / ~0.22 / 0.10

Usage: inject_tail_taper.py <in.mp4> <out.mp4> <tail_frames> <alpha> <alpha_end> <ramp>
"""
import subprocess, os, sys, random, glob, tempfile
from PIL import Image

vid, out = sys.argv[1], sys.argv[2]
tail_n, alpha, alpha_end, ramp = int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]), int(sys.argv[6])

td = tempfile.mkdtemp(prefix='injtaper_')
fr = os.path.join(td, 'f')
os.makedirs(fr)
subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', vid, os.path.join(fr, 'f_%04d.png')], check=True)

palette = [(185, 115, 215), (115, 195, 140), (150, 148, 162), (205, 150, 192), (138, 182, 148), (160, 120, 175)]
CW, CH = 36, 64
files = sorted(glob.glob(os.path.join(fr, '*.png')))
total = len(files)
start = total - tail_n

def alpha_for(pos_in_tail):
    """pos_in_tail: 0..tail_n-1"""
    from_end = tail_n - 1 - pos_in_tail
    if from_end >= ramp:
        return alpha
    # linear taper: last frame hits alpha_end
    return alpha + (alpha_end - alpha) * (ramp - from_end) / ramp

for i, f in enumerate(files):
    if i < start:
        continue
    a = alpha_for(i - start)
    frame = Image.open(f).convert('RGB')
    small = Image.new('RGB', (CW, CH))
    px = small.load()
    for y in range(CH):
        for x in range(CW):
            px[x, y] = random.choice(palette)
    noisy = small.resize(frame.size, Image.NEAREST)
    Image.blend(frame, noisy, a).save(f)
    if i >= total - ramp - 1:
        print(f"  frame {i}: alpha={a:.2f}")
print(f"injected frames {start}..{total-1} ({tail_n}f), taper last {ramp}f {alpha}->{alpha_end}")

probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=r_frame_rate', '-of', 'csv=p=0', vid],
                       capture_output=True, text=True).stdout.strip()
subprocess.run(['ffmpeg', '-y', '-v', 'error', '-framerate', probe, '-i', os.path.join(fr, 'f_%04d.png'),
                '-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', '-an', out], check=True)
print(f"output: {out}")
