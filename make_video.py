r"""Render a promo video of EchoScope: the real radar code driven by the demo scene (offline),
captions on top, the demo audio as the soundtrack. Needs ffmpeg on PATH.

    python make_video.py out_dir            -> out_dir\EchoScope-video.mp4 (1920x1080, 36 s)
                                               out_dir\EchoScope-short.mp4 (1080x1920, 30 s)
"""
import math
import os
import subprocess
import sys
import wave

import cairo
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import echoscope as es  # noqa: E402

FPS = 30
URL = 'github.com/karlisskotelis-cpu/echoscope'


class Collector:
    """Stand-in engine for the demo scene: keeps every chunk so it can be replayed and saved."""

    def __init__(self):
        self.chunks = []

    def push(self, st):
        self.chunks.append(np.array(st, dtype=np.float32))


def make_audio(seconds):
    col = Collector()
    scene = es.DemoScene(col, realtime=False)
    scene.render(seconds)
    audio = np.concatenate(col.chunks)[:int(seconds * es.DemoScene.RATE)]
    # gentle master level + fade out at the end
    audio = audio * 0.8
    n_fade = int(1.5 * es.DemoScene.RATE)
    audio[-n_fade:] *= np.linspace(1.0, 0.0, n_fade)[:, None]
    return np.clip(audio, -1, 1), col.chunks


def write_wav(path, audio):
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(es.DemoScene.RATE)
        w.writeframes((audio * 32767).astype('<i2').tobytes())


class RadarRenderer:
    """The real App drawing code on an offscreen surface, fed with the demo audio on a schedule."""

    def __init__(self, w, h, dpr, mode='petals', state_text='Speakers (your headset)'):
        self.app = es.App()
        S = self.app.S
        S.update({'mode': mode, 'sensitivity': 7, 'width': 7, 'centre': 5, 'smoothing': 0.5, 'maxCues': 3,
                  'colourByPitch': True, 'markers': True, 'boost': False, 'guides': True, 'meters': True,
                  'theme': 'cyan', 'lineWidth': 3.0, 'life': 0.45, 'opacity': 1.0, 'adaptive': False, 'fullCircle': False})
        self.app.W, self.app.H, self.app.dpr = w, h, dpr
        self.app.active = True
        self.app.engine.state = 'live'
        self.app.engine.rate = es.DemoScene.RATE
        self.app.engine.device_name = state_text
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        self.chunk_i = 0

    def feed(self, chunks, t):
        rate, chunk = es.DemoScene.RATE, es.DemoScene.CHUNK
        while self.chunk_i < len(chunks) and (self.chunk_i + 1) * chunk <= t * rate:
            self.app.engine.push(chunks[self.chunk_i])
            self.chunk_i += 1

    def frame(self, chunks, t):
        # two analysis steps per video frame, like the 60 Hz app loop
        for sub in (t - 0.5 / FPS, t):
            self.feed(chunks, sub)
            self.app.an.analyse(sub, self.app.S)
            self.app.an.prune(sub, self.app.S['life'])
        ctx = cairo.Context(self.surface)
        ctx.set_operator(cairo.OPERATOR_CLEAR)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)
        self.app.draw(ctx, t)
        self.surface.flush()
        return self.surface


def text(ctx, s, x, y, size, rgba, bold=True, align='center'):
    ctx.select_font_face('Segoe UI', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    ctx.set_font_size(size)
    ext = ctx.text_extents(s)
    if align == 'center':
        x -= ext.x_advance / 2
    elif align == 'right':
        x -= ext.x_advance
    ctx.set_source_rgba(*rgba)
    ctx.move_to(x, y)
    ctx.show_text(s)


def fade(t, t0, t1, ramp=0.5):
    if t < t0 or t > t1:
        return 0.0
    return min(1.0, (t - t0) / ramp, (t1 - t) / ramp)


def background(ctx, W, H, accent):
    g = cairo.LinearGradient(0, 0, 0, H)
    g.add_color_stop_rgb(0, 0.055, 0.075, 0.105)
    g.add_color_stop_rgb(1, 0.03, 0.04, 0.06)
    ctx.set_source(g)
    ctx.paint()
    r = cairo.RadialGradient(W / 2, H * 0.55, 0, W / 2, H * 0.55, max(W, H) * 0.6)
    r.add_color_stop_rgba(0, accent[0], accent[1], accent[2], 0.10)
    r.add_color_stop_rgba(1, accent[0], accent[1], accent[2], 0.0)
    ctx.set_source(r)
    ctx.paint()


def render(out_path, W, H, seconds, captions, radar_box, title_sz, cap_sz, mode_plan, wav_path, chunks):
    accent = es.THEMES['cyan']
    rx, ry, rw, rh = radar_box
    radar = RadarRenderer(rw, rh, rw / 460.0 * 1.0, mode=mode_plan[0][1])
    cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgra', '-s', '%dx%d' % (W, H), '-r', str(FPS),
           '-i', 'pipe:0', '-i', wav_path, '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
           '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    frame_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    n_frames = int(seconds * FPS)
    for k in range(n_frames):
        t = k / FPS
        for t_mode, mode in mode_plan:
            if t >= t_mode:
                radar.app.S['mode'] = mode
        rs = radar.frame(chunks, t)
        ctx = cairo.Context(frame_surface)
        background(ctx, W, H, accent)
        # radar panel with a soft shadow
        ctx.set_source_rgba(0, 0, 0, 0.45)
        es.App._rounded(ctx, rx + 6, ry + 10, rw, rh, 12)
        ctx.fill()
        ctx.set_source_surface(rs, rx, ry)
        ctx.paint()
        # title block
        a_title = fade(t, 0, seconds, 0.6)
        text(ctx, 'ECHOSCOPE', W / 2, ry - title_sz * 0.9, title_sz, (0.906, 0.929, 0.957, a_title))
        text(ctx, 'free audio radar for games', W / 2, ry - title_sz * 0.15, title_sz * 0.36, (accent[0], accent[1], accent[2], a_title))
        # captions
        for c0, c1, line1, line2 in captions:
            a = fade(t, c0, c1, 0.4)
            if a <= 0:
                continue
            y0 = ry + rh + cap_sz * 1.6
            text(ctx, line1, W / 2, y0, cap_sz, (0.906, 0.929, 0.957, a))
            if line2:
                text(ctx, line2, W / 2, y0 + cap_sz * 1.25, cap_sz * 0.78, (0.545, 0.604, 0.69, a), False)
        frame_surface.flush()
        proc.stdin.write(bytes(frame_surface.get_data()))
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit('ffmpeg failed for ' + out_path)
    print('wrote', out_path)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), 'release', 'video')
    os.makedirs(out_dir, exist_ok=True)
    # --- landscape, 36 s ---
    seconds = 36.0
    audio, chunks = make_audio(seconds)
    wav = os.path.join(out_dir, 'demo-audio.wav')
    write_wav(wav, audio)
    caps = [
        (2.5, 8.0, 'It hears the stereo audio your game already plays', 'nothing is recorded, nothing is uploaded'),
        (8.0, 13.5, 'Angle = where the sound is   ·   length = how loud', 'the white arrow marks the newest sound'),
        (13.5, 19.0, 'Amber = low rumble   ·   light = footsteps and hits', 'dots on the rim mark sudden sounds'),
        (19.0, 24.5, 'Five looks: Petals, Dots, Comet, Solid Fan, Single Line', 'Tab opens live tuning sliders'),
        (24.5, 29.5, 'Press O: click-through overlay on top of your game', 'Ctrl+Shift+O brings the panel back'),
        (29.5, 36.0, 'Free  ·  open source  ·  single EXE', URL),
    ]
    modes = [(0, 'petals'), (19.0, 'dots'), (21.0, 'comet'), (23.0, 'fan'), (26.0, 'petals')]
    rw, rh = 1200, 760
    render(os.path.join(out_dir, 'EchoScope-video.mp4'), 1920, 1080, seconds, caps, ((1920 - rw) // 2, 200, rw, rh), 96, 40, modes, wav, chunks)
    # --- vertical short, 30 s ---
    seconds = 30.0
    audio, chunks = make_audio(seconds)
    wav2 = os.path.join(out_dir, 'demo-audio-short.wav')
    write_wav(wav2, audio)
    caps = [
        (2.0, 7.0, 'Hears your game\'s stereo audio', 'nothing recorded or uploaded'),
        (7.0, 12.0, 'Angle = where   ·   length = how loud', 'white arrow = newest sound'),
        (12.0, 17.0, 'Amber = rumble   ·   light = footsteps', 'rim dots = sudden sounds'),
        (17.0, 22.0, 'Press O: overlay over your game', 'five looks, live tuning'),
        (22.0, 30.0, 'FREE  ·  open source', URL),
    ]
    modes = [(0, 'petals'), (17.0, 'fan'), (22.0, 'petals')]
    rw, rh = 1000, 700
    render(os.path.join(out_dir, 'EchoScope-short.mp4'), 1080, 1920, seconds, caps, ((1080 - rw) // 2, 560, rw, rh), 110, 46, modes, wav2, chunks)


if __name__ == '__main__':
    main()
