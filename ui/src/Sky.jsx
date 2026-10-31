import { useEffect, useRef } from "react";

// The background: a night sky of cumulus, ordered-dithered into four inks the
// way the reference picture is — navy, indigo, a lavender haze and lit cream —
// drawn at a coarse pixel size and scaled up without smoothing, drifting slowly.

const PX = 3;                    // CSS pixels per dither dot
const FPS = 6;
const INKS = [[14, 16, 48], [40, 45, 98], [142, 146, 184], [236, 233, 220]];
// 4×4 Bayer matrix, 0..1: the dot pattern of an ordered dither.
const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map((v) => (v + 0.5) / 16);

function hash(x, y) {
  const h = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
  return h - Math.floor(h);
}

function noise(x, y) {
  const xi = Math.floor(x), yi = Math.floor(y);
  const xf = x - xi, yf = y - yi;
  const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf);
  const a = hash(xi, yi), b = hash(xi + 1, yi);
  const c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}

function fbm(x, y) {
  let sum = 0, amp = 0.5;
  for (let o = 0; o < 4; o++) {
    sum += amp * noise(x, y);
    x = x * 2.1 + 3.7; y = y * 2.1 + 1.3; amp *= 0.5;
  }
  return sum / 0.9375;
}

const smooth = (a, b, x) => {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
};

// Brightness 0..1 at a point given in 0..1 of the window. Cloud banks heap up
// along the bottom and the right; a channel of night runs diagonally through
// the upper middle, as in the picture.
function brightness(nx, ny, aspect, t) {
  const x = nx * aspect * 2.4 + t, y = ny * 2.4;
  const warp = noise(x * 0.7 + 9, y * 0.7 - t * 0.5) - 0.5;
  const n = fbm(x + warp * 1.2, y + warp * 0.8);
  const channel = Math.exp(-(((ny - 0.18) - 0.45 * (nx - 0.15)) ** 2) / 0.03);
  const bank = 0.42 * ny + 0.12 * nx - 0.5 * channel;
  const cloud = smooth(0.55, 0.85, n + bank);
  // Lit from above: the tops of the billows are the brightest.
  const lit = cloud * (0.72 + 0.28 * smooth(0.2, 0.8, n));
  return 0.05 + 0.04 * (1 - ny) + lit * 0.94;
}

export default function Sky() {
  const ref = useRef(null);

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas.getContext("2d");
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let w = 0, h = 0, image = null, t = 0, timer = null;

    function resize() {
      w = Math.ceil(window.innerWidth / PX);
      h = Math.ceil(window.innerHeight / PX);
      canvas.width = w;
      canvas.height = h;
      image = ctx.createImageData(w, h);
      draw();
    }

    function draw() {
      const data = image.data, aspect = w / h, top = INKS.length - 1;
      for (let y = 0; y < h; y++) {
        for (let x = 0; x < w; x++) {
          const v = brightness(x / w, y / h, aspect, t) * top;
          const step = Math.floor(v) + (v % 1 > BAYER[(y & 3) * 4 + (x & 3)] ? 1 : 0);
          const ink = INKS[Math.min(top, step)];
          const i = (y * w + x) * 4;
          data[i] = ink[0]; data[i + 1] = ink[1]; data[i + 2] = ink[2]; data[i + 3] = 255;
        }
      }
      ctx.putImageData(image, 0, 0);
    }

    resize();
    window.addEventListener("resize", resize);
    if (!still) {
      timer = setInterval(() => {
        if (document.hidden) return;
        t += 0.0035;
        draw();
      }, 1000 / FPS);
    }
    return () => {
      window.removeEventListener("resize", resize);
      clearInterval(timer);
    };
  }, []);

  return <canvas className="sky" ref={ref} aria-hidden="true" />;
}
