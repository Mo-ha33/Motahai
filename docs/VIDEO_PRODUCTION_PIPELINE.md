# Autonomous Video Production AI Pipeline: Wesam.ai × Hermes MCP

**Component:** Creative Media & Video Production Extension  
**Brand:** Motahai AI Workforce (`employees.motahai.com`)  
**Host Platform:** Wesam.ai (Per-Employee Seat Architecture)  
**Supervisor / Hub:** Hermes Autonomous AI Agent (`https://hermes.motahai.com`) via MCP  
**Standard:** 10-Second Modular Block Calibration (Omni Prompts + Google Vids / Generative Video)

---

## 1. Executive Summary

The **Autonomous Video Production AI Pipeline** converts narrative video concepts and corporate briefs into enterprise-grade video production packages with mathematical timing precision.

Rather than relying on unconstrained LLM video generation, this pipeline enforces **deterministic block timing, strict word-per-minute audio calibration, 3D character consistency, and automated manifest compilation**.

```
┌───────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   VIDEO PIPELINE WORKFLOW                                         │
├──────────────────────────┬────────────────────────────────────┬───────────────────────────────────┤
│ 1. Narrative & Timing    │ 2. Brand & Visual Governance       │ 3. Manifest & Audio Sync          │
├──────────────────────────┼────────────────────────────────────┼───────────────────────────────────┤
│ • 10-second atomic blocks│ • Logo Safe-Zones (Top-Left 20%)   │ • Master Music Prompt (BPM curves)│
│ • 18–20 words per block  │ • Enforced HEX palettes (#D42429)  │ • Per-block JSON specifications   │
│ • 2-second audio buffer  │ • 3D Pixar / Corporate Omni Prompts│ • Time-synced .SRT Subtitles      │
└──────────────────────────┴────────────────────────────────────┴───────────────────────────────────┘
```

---

## 2. Mathematical Timing Calibration Model

The core failure mode of generative video tools (such as Google Vids, Runway, and Pika) is **audio-visual desynchronization**: text voiceovers overrun slide boundaries or cut off during transitions.

This pipeline enforces the **10-Second Modular Block Rule**:

$$\text{Block Duration} = 10\text{ seconds}$$
$$\text{Voiceover Duration} = 8\text{ seconds} \quad (\approx 18\text{ to } 20\text{ words at natural speech cadence})$$
$$\text{Buffer Window} = 2\text{ seconds} \quad (\text{reserved for scene transitions, SFX, and visual breathing room})$$

### 3-Minute Benchmark (18 Blocks):
- **Total Duration:** 180 seconds (18 blocks × 10 seconds).
- **Total Spoken Words:** ~324–360 words across 5 narrative acts:
  - **Act 1:** The Origin & The Spark (Blocks 01–04)
  - **Act 2:** Building Trust & Core Technology (Blocks 05–08)
  - **Act 3:** Global Expansion & Scale (Blocks 09–12)
  - **Act 4:** People, Culture & Empowerment (Blocks 13–15)
  - **Act 5:** Milestones & The Grand Finale (Blocks 16–18)

---

## 3. Brand Governance & "Omni" Prompt Architecture

Each block generates a standardized **Omni Prompt** consumed by text-to-image/video models (Midjourney v6, Google Veo, Runway Gen-3):

### Omni Prompt Structure:
```
[Style Anchor]: High-end 3D corporate animation style / Pixar 3D aesthetic
[Character Reference]: Accurate facial likeness anchored to uploaded assets, specific de-aging (e.g. to 2011)
[Color Palette]: Strict HEX enforcement (e.g., Flag Red #D42429, Navy #0B1B3D, Studio Grey #F5F5F5)
[Camera & Lighting]: 35mm cinematic lens, shallow depth of field, natural or dramatic studio lighting
[Exclusions & Flags]: Negative prompts (--no women/females where founder consistency is required, --no suits)
```

---

## 4. Hermes MCP Server Tool Contracts

When deployed as an MCP service on the Hermes VPS node, the following tools are exposed to the Wesam.ai AI Employee:

### 1. `calibrate_video_blocks`
- **Purpose:** Splits script drafts into mathematically balanced 10s chunks.
- **Inputs:** `script_draft` (string), `target_duration_seconds` (integer), `target_wpm` (integer, default 130).
- **Outputs:** Array of blocks with calculated word counts, expected speech duration, and overflow warnings.

### 2. `enforce_brand_rules`
- **Purpose:** Audits prompt text against brand guidelines before rendering.
- **Validates:** Brand HEX consistency, logo safe zone rules, banned clothing/tones.

### 3. `generate_omni_prompts`
- **Purpose:** Synthesizes calibrated scene descriptions into 3D/photorealistic prompts with lighting and camera parameters.

### 4. `build_production_manifest`
- **Purpose:** Compiles production assets into deterministic deliverables:
  - `Block_XX.json` manifests containing voiceover text, timing, visual prompt, and SFX cues.
  - Synchronized `.srt` subtitle file.
  - Master music prompt with BPM crescendo curves (e.g. 115 BPM).

---

## 5. Commercial Integration on Wesam.ai

- **Role Persona:** *AI Creative Video Director*
- **Delivery Model:**
  - **Self-Serve:** The AI Employee conducts the creative brief in Wesam chat and outputs a full Production ZIP within 3 minutes.
  - **Retainer Tier:** Monthly quota for corporate communications, internal HR updates, and marketing teasers.
