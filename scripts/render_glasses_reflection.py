"""Rebuild the sunglasses reflections in Assets/Background.mp4.

Requires numpy, opencv-python and ffmpeg. Run from the repository root:
    python scripts/render_glasses_reflection.py --ffmpeg ffmpeg --output Assets/Background-keys.mp4

The rotoscoped lens outlines are fitted to the 24 fps source. Reflections feather
inwards from those outlines, preserving the metal rims, nose and cheek lighting.
"""

import argparse
import subprocess
from pathlib import Path

import cv2
import numpy as np


SOURCE = Path('Assets/Background.mp4')
# Frame, eight clockwise contour landmarks per lens (starting at the top), yaw.
# These aviator outlines deliberately follow the asymmetric nasal edges rather
# than using ellipses, which painted over the bridge and nose in the old render.
# Collapsed contours mark lenses whose reflective fronts are hidden.
TRACK = [
    (0, [(559,193),(582,198),(589,216),(575,242),(551,255),(534,247),(528,224),(536,202)],
        [(640,188),(661,196),(674,219),(665,242),(641,254),(619,244),(605,220),(615,198)], -0.13),
    (24, [(576,193),(600,200),(610,219),(597,244),(573,255),(552,246),(545,221),(553,201)],
         [(660,192),(682,200),(695,222),(684,247),(660,256),(640,245),(626,220),(637,200)], 0.0),
    (48, [(601,191),(626,199),(638,218),(625,242),(600,253),(579,244),(570,221),(579,200)],
         [(683,194),(706,203),(715,225),(705,247),(683,257),(663,246),(650,221),(661,201)], 0.16),
    (60, [(624,188),(646,197),(654,217),(642,239),(619,251),(601,244),(594,220),(603,198)],
         [(694,194),(718,203),(726,225),(717,248),(698,257),(686,245),(670,222),(682,201)], 0.34),
    (66, [(638,187),(654,195),(663,215),(652,239),(634,251),(619,242),(611,218),(619,197)],
         [(695,194),(719,201),(732,226),(724,249),(709,257),(695,244),(678,221),(684,202)], 0.46),
    (72, [(652,187),(669,195),(680,215),(669,240),(649,251),(634,243),(629,218),(637,196)],
         [(703,194),(724,202),(736,226),(728,250),(716,256),(705,242),(693,220),(697,202)], 0.62),
    (78, [(672,187),(686,198),(695,218),(686,241),(671,252),(660,242),(651,217),(658,196)],
         [(709,193),(729,202),(736,226),(727,250),(716,254),(711,242),(704,220),(705,201)], 0.77),
    (84, [(691,190),(705,200),(713,220),(704,242),(690,252),(681,243),(675,218),(680,198)],
         [(714,194),(729,203),(732,226),(729,240),(725,248),(720,236),(713,216),(712,201)], 0.92),
    (90, [(710,192),(716,202),(719,220),(716,242),(708,253),(704,241),(701,219),(704,200)],
         [(734,198)] * 8, 1.05),
    (96, [(724,218)] * 8, [(734,218)] * 8, 1.20),
    (138, [(528,222)] * 8, [(535,219)] * 8, -1.20),
    (144, [(528,204),(530,211),(530,226),(526,235),(521,243),(521,234),(523,219),(525,211)],
          [(538,194),(542,202),(544,222),(542,244),(537,253),(533,242),(532,221),(534,202)], -1.05),
    (150, [(528,205),(533,210),(535,222),(527,235),(518,249),(516,242),(515,228),(519,213)],
          [(552,193),(562,201),(566,220),(561,243),(551,254),(541,244),(537,220),(541,201)], -0.9),
    (156, [(530,202),(541,209),(545,220),(536,237),(522,257),(517,249),(515,228),(520,210)],
          [(573,190),(584,199),(589,219),(583,243),(570,254),(555,244),(550,220),(557,199)], -0.74),
    (162, [(535,197),(549,206),(554,222),(546,239),(531,258),(519,248),(515,227),(520,207)],
          [(586,188),(605,198),(609,219),(602,243),(583,254),(567,241),(562,218),(570,198)], -0.58),
    (168, [(544,196),(558,204),(564,220),(555,239),(539,258),(525,249),(519,227),(526,207)],
          [(603,187),(625,196),(634,217),(624,242),(601,253),(580,242),(573,219),(581,198)], -0.44),
    (180, [(555,194),(573,201),(582,219),(570,241),(550,255),(531,245),(525,224),(534,203)],
          [(631,187),(651,195),(661,217),(651,242),(628,254),(606,244),(595,219),(604,197)], -0.23),
    (192, [(559,193),(582,198),(589,216),(575,242),(551,255),(534,247),(528,224),(536,202)],
          [(640,188),(661,196),(674,219),(665,242),(641,254),(619,244),(605,220),(615,198)], -0.13),
]


def track_slopes():
    """Monotone cubic slopes avoid sharp speed changes between track points."""
    times = np.array([row[0] for row in TRACK], dtype=np.float64)
    values = np.array([(*np.ravel(row[1]), *np.ravel(row[2]), row[3]) for row in TRACK], dtype=np.float64)
    intervals = np.diff(times)
    secants = np.diff(values, axis=0) / intervals[:, None]
    slopes = np.zeros_like(values)
    for index in range(1, len(TRACK) - 1):
        previous, following = secants[index - 1], secants[index]
        same_direction = previous * following > 0
        first_weight = 2 * intervals[index] + intervals[index - 1]
        second_weight = intervals[index] + 2 * intervals[index - 1]
        denominator = np.zeros_like(previous)
        np.divide(first_weight, previous, out=denominator, where=same_direction)
        extra = np.zeros_like(following)
        np.divide(second_weight, following, out=extra, where=same_direction)
        np.divide(first_weight + second_weight, denominator + extra, out=slopes[index], where=same_direction)
    # The video loops: use the same tangent on both sides of its seam.
    previous, following = secants[-1], secants[0]
    same_direction = previous * following > 0
    denominator = np.zeros_like(previous)
    np.divide(1, previous, out=denominator, where=same_direction)
    extra = np.zeros_like(following)
    np.divide(1, following, out=extra, where=same_direction)
    np.divide(2, denominator + extra, out=slopes[0], where=same_direction)
    slopes[-1] = slopes[0]
    return times, values, slopes


TRACK_TIMES, TRACK_VALUES, TRACK_SLOPES = track_slopes()


def track_at(frame):
    if not TRACK_TIMES[0] <= frame <= TRACK_TIMES[-1]:
        raise ValueError(f'Frame {frame} is outside the calibrated track')
    index = min(np.searchsorted(TRACK_TIMES, frame, side='right') - 1, len(TRACK) - 2)
    duration = TRACK_TIMES[index + 1] - TRACK_TIMES[index]
    t = (frame - TRACK_TIMES[index]) / duration
    values = ((2*t**3 - 3*t**2 + 1) * TRACK_VALUES[index]
              + (t**3 - 2*t**2 + t) * duration * TRACK_SLOPES[index]
              + (-2*t**3 + 3*t**2) * TRACK_VALUES[index + 1]
              + (t**3 - t**2) * duration * TRACK_SLOPES[index + 1])
    return values[:16].reshape(8, 2), values[16:32].reshape(8, 2), values[32]


def key_texture():
    """A pair of crossed, open-bow keys, drawn as an environment reflection."""
    size = 512
    key = np.zeros((size, size), np.uint8)
    # Draw one key vertically, then reflect and rotate it into a crossed pair.
    cv2.ellipse(key, (256, 344), (29, 35), 0, 0, 360, 255, 11, cv2.LINE_AA)
    cv2.circle(key, (222, 344), 13, 255, 9, cv2.LINE_AA)
    cv2.circle(key, (290, 344), 13, 255, 9, cv2.LINE_AA)
    cv2.line(key, (256, 307), (256, 111), 255, 14, cv2.LINE_AA)
    cv2.line(key, (256, 119), (294, 119), 255, 14, cv2.LINE_AA)
    cv2.line(key, (287, 119), (287, 140), 255, 12, cv2.LINE_AA)
    cv2.line(key, (256, 151), (284, 151), 255, 12, cv2.LINE_AA)
    cv2.line(key, (278, 151), (278, 166), 255, 10, cv2.LINE_AA)
    result = np.zeros_like(key)
    for angle in (-55, 55):
        matrix = cv2.getRotationMatrix2D((256, 256), angle, 0.72)
        result = np.maximum(result, cv2.warpAffine(key, matrix, (size, size), flags=cv2.INTER_LINEAR))
    return result.astype(np.float32) / 255


def smooth_outline(points):
    """Closed Catmull-Rom contour through the eight rotoscoped landmarks."""
    t = np.linspace(0, 1, 16, endpoint=False)[:, None]
    segments = []
    for index in range(len(points)):
        p0, p1, p2, p3 = [points[i % len(points)] for i in range(index-1, index+3)]
        segments.append(0.5 * (2*p1 + (-p0+p2)*t
                              + (2*p0-5*p1+4*p2-p3)*t*t
                              + (-p0+3*p1-3*p2+p3)*t*t*t))
    return np.concatenate(segments).astype(np.float32)


def lens_mask(lens, shape):
    """Supersampled, inset alpha: its feather never spills outside the glass."""
    outline = smooth_outline(lens)
    low, high = outline.min(axis=0), outline.max(axis=0)
    if high[0] - low[0] < 3 or high[1] - low[1] < 3:
        return None
    x0, y0 = np.maximum(0, np.floor(low - 3).astype(int))
    x1, y1 = np.minimum([shape[1], shape[0]], np.ceil(high + 4).astype(int))
    scale = 4
    coverage = np.zeros(((y1-y0)*scale, (x1-x0)*scale), np.uint8)
    polygon = np.round((outline - [x0, y0])*scale).astype(np.int32)
    cv2.fillPoly(coverage, [polygon], 255)
    inside = cv2.distanceTransform(coverage, cv2.DIST_L2, cv2.DIST_MASK_PRECISE) / scale
    alpha = np.clip((inside - 0.8) / 1.4, 0, 1)
    alpha = alpha*alpha*(3 - 2*alpha)
    alpha = cv2.resize(alpha, (x1-x0, y1-y0), interpolation=cv2.INTER_AREA)
    return (x0, y0, x1, y1), alpha, low, high


def composite_lens(frame, lens, yaw, texture, side):
    mask = lens_mask(lens, frame.shape)
    if mask is None:
        return
    (x0, y0, x1, y1), alpha, low, high = mask
    cx, cy = (low + high) / 2
    rx, ry = (high - low) / 2
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    u, v = (xx-cx)/rx, (yy-cy)/ry
    region = frame[y0:y1, x0:x1].astype(np.float32)

    # Keep the original broad illumination and top-of-glass glare, but suppress
    # the old skyline detail. The copper/red palette matches the cheek caustic.
    blurred = cv2.GaussianBlur(region, (0, 0), 5)
    luminance = cv2.cvtColor(blurred, cv2.COLOR_BGR2GRAY)
    row_light = np.maximum(luminance.mean(axis=1, keepdims=True), 20)
    illumination = np.clip(luminance / row_light, 0.65, 1.35)
    stops = [-1.1, -0.65, -0.1, 0.4, 1.1]
    colors = np.array([[62,70,82], [43,65,104], [23,64,163],
                       [18,40,100], [14,23,43]], dtype=np.float32)
    glass = np.stack([np.interp(v, stops, colors[:, channel]) for channel in range(3)], axis=-1)
    edge = np.clip(u*u + v*v, 0, 1)
    glass *= (illumination * (1 - 0.13*edge))[..., None]
    glass = 0.72*glass + 0.28*blurred*np.array([0.65,0.8,1], dtype=np.float32)
    replace = np.clip((v + 0.75)/0.45, 0, 1)
    replace = replace*replace*(3 - 2*replace)
    glass = region*(1-replace[..., None]) + glass*replace[..., None]

    # Reflection scale is fixed relative to the glass. Only gentle parallax and
    # lens curvature change as the head turns; a slight optical blur softens it.
    radial = 1 + 0.065*(u*u + v*v)
    tx = 256 + 178*(u*radial + 0.10*yaw + 0.025*yaw*v) + side*5
    ty = 256 + 164*(v*radial + 0.025*yaw*u)
    keys = cv2.remap(texture, tx.astype(np.float32), ty.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
    keys = cv2.GaussianBlur(keys, (0, 0), 0.65)
    reflection = keys*(0.28 + 0.07*np.cos(yaw))*(1 - 0.35*edge)*illumination
    # Screen blending retains the underlying glass shading instead of laying
    # opaque white lettering over it. Warm reflected light is intentionally dim.
    reflected_light = np.array([50,130,220], dtype=np.float32)
    glass += (255-glass)*(reflected_light/255)*reflection[..., None]

    top = np.clip((-v + 0.08)/0.7, 0, 1)
    glare = np.clip((cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)-125)/95, 0, 0.75)*top
    detail = (region-blurred)*top[..., None]*0.45
    glass += detail
    glass = glass*(1-glare[..., None]) + region*glare[..., None]

    # At oblique angles retain the filmed transmission and the foreground metal
    # arms. Neutral source pixels reveal the face/background through the glass;
    # treating these as an opaque colored disc was especially conspicuous.
    oblique = np.clip((abs(yaw)-0.35)/0.55, 0, 1)
    saturation = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)[:, :, 1]/255
    transmission = oblique*(1-np.clip((saturation-0.20)/0.45, 0, 1))
    opacity = 0.96*(1-transmission)
    alpha *= opacity
    blended = np.clip(region*(1-alpha[..., None]) + glass*alpha[..., None], 0, 255).astype(np.uint8)
    # Write only covered pixels so untouched face/rim pixels are bit-identical
    # before encoding (including protection from float round-off).
    target = frame[y0:y1, x0:x1]
    target[alpha > 0] = blended[alpha > 0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()

    capture = cv2.VideoCapture(str(SOURCE))
    if not capture.isOpened():
        raise RuntimeError(f'Cannot open {SOURCE}')
    width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS)
    if (width, height) != (1280, 720) or abs(fps - 24) > 0.01:
        raise RuntimeError('The lens track requires the original 1280x720, 24 fps footage')
    command = [args.ffmpeg, '-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
               '-s', f'{width}x{height}', '-r', '24', '-i', '-', '-i', str(SOURCE),
               '-map', '0:v:0', '-map', '1:a?', '-c:v', 'libx264', '-crf', '17',
               '-preset', 'medium', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', args.output]
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE)
    texture = key_texture()
    count = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            left, right, yaw = track_at(count)
            composite_lens(frame, left, yaw, texture, -1)
            composite_lens(frame, right, yaw, texture, 1)
            encoder.stdin.write(frame.tobytes())
            count += 1
    finally:
        capture.release()
        encoder.stdin.close()
    if encoder.wait() != 0:
        raise RuntimeError('ffmpeg failed to encode the video')
    print(f'Rendered {count} frames to {args.output}')


if __name__ == '__main__':
    main()
