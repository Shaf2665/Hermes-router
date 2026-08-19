import sharp from 'sharp';
import { fileURLToPath } from 'node:url';

const svg = Buffer.from(`
<svg width="1200" height="630" viewBox="0 0 1200 630" xmlns="http://www.w3.org/2000/svg">
  <rect width="1200" height="630" fill="#071522"/>
  <path d="M0 515C190 430 275 640 520 505S850 365 1200 480V630H0Z" fill="#002b33"/>
  <path d="M660 98H1110" stroke="#0b93ad" stroke-width="2" opacity=".65"/>
  <text x="92" y="116" fill="#a5e4f0" font-family="Arial, sans-serif" font-size="22" font-weight="700" letter-spacing="3">SELF-HOSTED AI GATEWAY</text>
  <text x="92" y="278" fill="#ffffff" font-family="Arial, sans-serif" font-size="84" font-weight="700">Hermes Router</text>
  <text x="92" y="356" fill="#d7e7eb" font-family="Arial, sans-serif" font-size="38" font-weight="600">One endpoint. Multiple AI providers.</text>
  <text x="92" y="408" fill="#d7e7eb" font-family="Arial, sans-serif" font-size="38" font-weight="600">Automatic failover.</text>
  <rect x="92" y="480" width="1016" height="1" fill="#0b93ad" opacity=".65"/>
  <text x="92" y="538" fill="#a5e4f0" font-family="Arial, sans-serif" font-size="26">Your App</text>
  <text x="246" y="538" fill="#7fd1df" font-family="Arial, sans-serif" font-size="26">→</text>
  <text x="287" y="538" fill="#ffffff" font-family="Arial, sans-serif" font-size="26" font-weight="700">Hermes</text>
  <text x="403" y="538" fill="#7fd1df" font-family="Arial, sans-serif" font-size="26">→</text>
  <text x="444" y="538" fill="#a5e4f0" font-family="Arial, sans-serif" font-size="26">Gemini · Groq · OpenRouter · Ollama</text>
</svg>`);

const logo = await sharp(fileURLToPath(new URL('../src/assets/logo.png', import.meta.url)))
	.resize(118, 118, { fit: 'contain' })
	.png()
	.toBuffer();

await sharp(svg)
	.composite([{ input: logo, left: 958, top: 70 }])
	.png({ compressionLevel: 9, palette: true })
	.toFile(fileURLToPath(new URL('../public/og-image.png', import.meta.url)));
