"""Rebuild the sunglasses reflections in Assets/Background.mp4.

Requires numpy, opencv-python and ffmpeg. Run from the repository root:
    python scripts/render_glasses_reflection.py --ffmpeg ffmpeg --output Assets/Background-keys.mp4

The lens tracks are hand-fitted to the 24 fps source. Coordinates describe the
inside of the original metal rims, so the original frames and arms stay visible.
"""

import argparse
import subprocess
from pathlib import Path

import cv2
import numpy as np


SOURCE = Path('Assets/Background.mp4')
# frame, left lens (center x/y, half width/height), right lens, approximate yaw
TRACK = [
    (0,   (560, 219, 29, 31), (639, 218, 33, 32), -0.13),
    (6,   (566, 219, 30, 32), (648, 218, 33, 33), -0.08),
    (12,  (570, 219, 31, 33), (652, 218, 34, 33), -0.04),
    (18,  (574, 219, 32, 33), (657, 218, 34, 33), 0.0),
    (24,  (578, 219, 33, 34), (661, 219, 35, 34), 0.0),
    (30,  (583, 219, 33, 34), (667, 219, 35, 34), 0.02),
    (36,  (588, 219, 33, 34), (672, 219, 35, 34), 0.05),
    (42,  (595, 219, 33, 34), (678, 220, 34, 34), 0.1),
    (48,  (603, 219, 33, 34), (685, 220, 33, 34), 0.16),
    (54,  (612, 217, 32, 34), (690, 221, 32, 34), 0.23),
    (60,  (621, 216, 31, 33), (697, 222, 29, 33), 0.34),
    (66,  (636, 215, 29, 33), (705, 222, 26, 33), 0.46),
    (72,  (651, 214, 27, 33), (712, 221, 23, 33), 0.62),
    (78,  (669, 213, 24, 33), (719, 221, 19, 33), 0.77),
    (84,  (689, 213, 15, 33), (728, 220, 13, 33), 0.92),
    (90,  (678, 216, 0, 30),  (730, 217, 0, 30), 1.05),
    (144, (534, 220, 0, 30),  (539, 218, 0, 30), -1.05),
    (150, (524, 222, 8, 32),  (554, 219, 12, 33), -0.9),
    (156, (526, 222, 15, 32), (571, 218, 20, 33), -0.74),
    (162, (534, 222, 20, 32), (589, 218, 24, 33), -0.58),
    (168, (540, 221, 23, 32), (603, 218, 29, 33), -0.44),
    (174, (549, 220, 27, 32), (617, 218, 31, 33), -0.31),
    (180, (555, 220, 28, 32), (628, 217, 32, 33), -0.23),
    (186, (559, 220, 29, 32), (635, 218, 33, 33), -0.16),
    (192, (560, 219, 29, 31), (639, 218, 33, 32), -0.13),
]


def track_at(frame):
    for a, b in zip(TRACK, TRACK[1:]):
        if a[0] <= frame <= b[0]:
            t = (frame - a[0]) / (b[0] - a[0])
            left = np.array(a[1]) * (1 - t) + np.array(b[1]) * t
            right = np.array(a[2]) * (1 - t) + np.array(b[2]) * t
            return left, right, a[3] * (1 - t) + b[3] * t
    raise ValueError(f'Frame {frame} is outside the calibrated track')


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
        matrix = cv2.getRotationMatrix2D((256, 256), angle, 0.84)
        result = np.maximum(result, cv2.warpAffine(key, matrix, (size, size), flags=cv2.INTER_LINEAR))
    return result.astype(np.float32) / 255


def composite_lens(frame, lens, yaw, texture, strength, side):
    cx, cy, rx, ry = lens
    if rx < 2:
        return
    # The outer halves of aviator lenses wrap slightly away from the face.
    yaw += side * 0.05
    cy += 1.5
    x0, x1 = max(0, int(cx-rx-2)), min(frame.shape[1], int(cx+rx+3))
    y0, y1 = max(0, int(cy-ry-2)), min(frame.shape[0], int(cy+ry+3))
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    u, v = (xx-cx)/rx, (yy-cy)/ry

    # Rounded aviator shape, narrowing slightly towards the bottom. The soft
    # one-pixel edge preserves the original metal rim and avoids a hard sticker.
    tapered_u = u / np.maximum(0.85, 1 - 0.08 * np.maximum(v, 0))
    distance = np.sqrt(tapered_u*tapered_u + v*v)
    alpha = np.clip((1.0 - distance) * min(rx, ry) + 0.25, 0, 1) * strength

    # Convex lens normals, turned with the glasses. Reflect a fixed view ray
    # into a stationary image plane; this gives the keys curvature and parallax.
    nx = u * 0.22
    ny = v * 0.19
    nz = np.sqrt(np.maximum(0.1, 1 - nx*nx - ny*ny))
    c, s = np.cos(yaw), np.sin(yaw)
    world_x = nx*c + nz*s
    world_z = nz*c - nx*s
    reflected_x = 2*world_z*world_x
    reflected_y = 2*world_z*ny
    reflected_z = np.maximum(0.25, 2*world_z*world_z - 1)
    # Limit the projection at grazing angles; the keys naturally slide out of
    # view there, while the lens itself stays shaded.
    # Keep the reflected object at a finite distance from the glasses. As the
    # camera moves it travels across the curved lens instead of sticking to it.
    center_reflection = 2*np.sin(yaw)*np.cos(yaw) / max(0.25, 2*np.cos(yaw)**2 - 1)
    tx = 256 + 310 * (reflected_x/reflected_z - 0.8*center_reflection) + side*12
    ty = 256 + 310 * (reflected_y/reflected_z)
    keys = cv2.remap(texture, tx.astype(np.float32), ty.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)

    # A single fixed upper-left light illuminates the bronze-tinted glass and
    # the reflected keys. The specular term moves as the lens turns.
    light = np.array([-0.38, -0.30, 0.87], dtype=np.float32)
    light /= np.linalg.norm(light)
    diffuse = np.clip(world_x*light[0] + ny*light[1] + world_z*light[2], 0, 1)
    halfway = light + np.array([0, 0, 1], dtype=np.float32)
    halfway /= np.linalg.norm(halfway)
    specular_dot = np.clip(world_x*halfway[0] + ny*halfway[1] + world_z*halfway[2], 0, 1)
    specular = 0.22*specular_dot**10 + 0.78*specular_dot**100
    edge = np.clip(distance, 0, 1)
    shade = 0.60 + 0.40*diffuse - 0.22*edge*edge
    # BGR, matching the sculpture's cool highlights and warm shadows.
    glass = np.stack((43*shade, 49*shade, 53*shade), axis=-1)
    key_light = keys * (0.55 + 0.45*diffuse) * (1 - 0.25*edge)
    glass += key_light[..., None] * np.array([57, 77, 88], dtype=np.float32)
    glass += specular[..., None] * np.array([110, 121, 126], dtype=np.float32)
    region = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = np.clip(region*(1-alpha[..., None]) + glass*alpha[..., None], 0, 255).astype(np.uint8)


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
            # The skyline disappears when the glasses turn edge-on. Blend back
            # to the original side view, where the rims become partly occluded.
            strength = 1
            if 80 < count <= 84:
                strength = (84-count)/4
            elif 84 < count < 150:
                strength = 0
            elif 150 <= count < 156:
                strength = (count-150)/6
            composite_lens(frame, left, yaw, texture, strength, -1)
            composite_lens(frame, right, yaw, texture, strength, 1)
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
