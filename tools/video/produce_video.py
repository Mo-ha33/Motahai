import os
import sys
import subprocess
import json
import time

def run_cmd(cmd_args, check=True):
    if isinstance(cmd_args, str):
        print(f"[CMD] {cmd_args}")
        res = subprocess.run(cmd_args, shell=True, capture_output=True, text=True)
    else:
        print(f"[CMD] {' '.join(cmd_args)}")
        res = subprocess.run(cmd_args, capture_output=True, text=True)
    if res.returncode != 0 and check:
        print(f"[ERROR] {res.stderr}")
        raise RuntimeError(f"Command failed\n{res.stderr}")
    return res

BLOCKS = [
    {
        "id": "B01",
        "name": "Pressure",
        "title": "WORK MOVES FAST.",
        "subtitle": "Your business moves fast. Your workforce should move faster--without adding headcount, handoffs, operational drag, or another layer of management.",
        "start": 0.0,
        "end": 10.0,
        "vo_end": 8.0,
        "transition": "Music breath + red signal wipe into Motahai core"
    },
    {
        "id": "B02",
        "name": "Reveal",
        "title": "ONE OBJECTIVE. -> AUTONOMOUS EXECUTION.",
        "subtitle": "Meet Motahai: an autonomous AI workforce that understands objectives, plans the work, and executes across your business around the clock.",
        "start": 10.0,
        "end": 20.0,
        "vo_end": 18.0,
        "transition": "Agent nodes deploy on beat -> node wipe into next scene"
    },
    {
        "id": "B03",
        "name": "Orchestrate",
        "title": "COORDINATED BY DESIGN.",
        "subtitle": "Every agent collaborates, checks context, and hands off intelligently--turning complex workflows into coordinated action with full operational visibility.",
        "start": 20.0,
        "end": 30.0,
        "vo_end": 28.0,
        "transition": "Synchronized handoff pulse travels through every node"
    },
    {
        "id": "B04",
        "name": "Execute",
        "title": "RESEARCH * OPERATIONS * MARKETING * ANALYTICS",
        "subtitle": "From research and customer operations to marketing and analytics, Motahai transforms repetitive processes into dependable, scalable execution at enterprise speed.",
        "start": 30.0,
        "end": 40.0,
        "vo_end": 38.0,
        "transition": "Four functional streams converge into completed execution cube"
    },
    {
        "id": "B05",
        "name": "Govern",
        "title": "CONTROL WITHOUT BOTTLENECKS.",
        "subtitle": "You stay in command with clear approvals, traceable decisions, and measurable outcomes--while autonomous agents handle the operational load.",
        "start": 40.0,
        "end": 50.0,
        "vo_end": 48.0,
        "transition": "Approval ring closes, audit trail locks into stable horizon"
    },
    {
        "id": "B06",
        "name": "Outcome",
        "title": "MULTIPLY YOUR CAPACITY. -> MOTAHAI",
        "subtitle": "Move from scattered tasks to an AI-native operation. Deploy Motahai, multiply your capacity, and let your business run ahead.",
        "start": 50.0,
        "end": 60.0,
        "vo_end": 58.0,
        "transition": "Wordmark hold with final impact at second 59 and clean tail"
    }
]

def generate_voiceovers(work_dir):
    print("=== Step 1: Synthesizing Voiceover Tracks (SAPI 120 BPM Calibrated) ===")
    ps_script_path = os.path.join(work_dir, "gen_tts.ps1")
    
    ps_content = """Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = 0
"""
    for idx, b in enumerate(BLOCKS):
        wav_out = os.path.join(work_dir, f"raw_vo_{b['id']}.wav").replace("\\", "/")
        text = b["subtitle"].replace("'", "''").replace('"', '`"')
        ps_content += f"""
$synth.SetOutputToWaveFile('{wav_out}')
$synth.Speak('{text}')
"""
    ps_content += """$synth.Dispose()
Write-Host 'TTS Synthesis Complete'
"""
    with open(ps_script_path, "w", encoding="utf-8") as f:
        f.write(ps_content)

    run_cmd(f'powershell -ExecutionPolicy Bypass -File "{ps_script_path}"')

    # Pad each block to exactly 10.0 seconds
    padded_wavs = []
    for b in BLOCKS:
        raw_wav = os.path.join(work_dir, f"raw_vo_{b['id']}.wav")
        pad_wav = os.path.join(work_dir, f"pad_vo_{b['id']}.wav")
        cmd = [
            "ffmpeg", "-y",
            "-i", raw_wav,
            "-af", "atrim=end=8.0,apad=whole_dur=10.0",
            "-ar", "44100",
            "-ac", "2",
            pad_wav
        ]
        run_cmd(cmd)
        padded_wavs.append(pad_wav)

    # Concat all 6 voiceover tracks
    concat_list = os.path.join(work_dir, "vo_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for pw in padded_wavs:
            f.write(f"file '{os.path.abspath(pw).replace(os.sep, '/')}'\n")

    master_vo = os.path.join(work_dir, "master_voiceover.wav")
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", master_vo])
    return master_vo

def generate_music_track(work_dir):
    print("=== Step 2: Generating 120 BPM Master Music Architecture ===")
    music_wav = os.path.join(work_dir, "master_music.wav")
    filter_expr = (
        "aevalsrc="
        "'0.20*sin(2*PI*75*t)*exp(-25*mod(t,0.5))"
        "+0.12*random(0)*exp(-30*mod(t-0.5,1.0))"
        "+0.06*random(1)*exp(-60*mod(t,0.25))"
        "+0.14*sin(2*PI*73.416*t)*exp(-5*mod(t,0.5))"
        "+0.08*sin(2*PI*293.66*t)*exp(-10*mod(t,0.25))':s=44100:d=60"
    )
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", filter_expr, "-ar", "44100", "-ac", "2", music_wav]
    run_cmd(cmd)
    return music_wav

def mix_master_audio(work_dir, master_vo, master_music):
    print("=== Step 3: Mixing Audio Bed (Ducking Music Under Voiceover) ===")
    mixed_audio = os.path.join(work_dir, "master_mix.wav")
    cmd = [
        "ffmpeg", "-y",
        "-i", master_vo,
        "-i", master_music,
        "-filter_complex", "[0:a]volume=1.2[vo];[1:a]volume=0.35[mus];[vo][mus]amix=inputs=2:duration=first:dropout_transition=2[out]",
        "-map", "[out]",
        "-ar", "44100",
        "-ac", "2",
        mixed_audio
    ]
    run_cmd(cmd)
    return mixed_audio

def generate_video_blocks(work_dir):
    print("=== Step 4: Rendering 10-Second 3D/Brand Animatic Blocks ===")
    rendered_blocks = []

    for idx, b in enumerate(BLOCKS):
        block_mp4 = os.path.join(work_dir, f"block_{b['id']}.mp4")
        
        # Write text files to avoid escaping issues
        title_file = os.path.join(work_dir, f"title_{b['id']}.txt")
        sub_file = os.path.join(work_dir, f"sub_{b['id']}.txt")
        trans_file = os.path.join(work_dir, f"trans_{b['id']}.txt")
        meta_file = os.path.join(work_dir, f"meta_{b['id']}.txt")
        
        with open(title_file, "w", encoding="utf-8") as f:
            f.write(b["title"])
        with open(sub_file, "w", encoding="utf-8") as f:
            f.write(f'VO (0s-8s): "{b["subtitle"]}"')
        with open(trans_file, "w", encoding="utf-8") as f:
            f.write(f'>>> [2s BUFFER · {b["transition"].upper()}] <<<')
        with open(meta_file, "w", encoding="utf-8") as f:
            f.write(f'BLOCK {idx+1} OF 6 | TIMECODE 00:{idx*10:02d}:00 - 00:{(idx+1)*10:02d}:00 | BARS {idx*5+1}-{idx*5+5} | 24 FPS')

        title_path_ff = title_file.replace("\\", "/").replace(":", "\\:")
        sub_path_ff = sub_file.replace("\\", "/").replace(":", "\\:")
        trans_path_ff = trans_file.replace("\\", "/").replace(":", "\\:")
        meta_path_ff = meta_file.replace("\\", "/").replace(":", "\\:")

        filters = [
            # Top header bar (Brand Safe Zone)
            "drawbox=x=60:y=60:w=500:h=60:color=0xD42429@0.85:t=fill",
            "drawtext=text='MOTAHAI AUTONOMOUS AI WORKFORCE':fontcolor=0xFFFFFF:fontsize=22:x=85:y=82",
            
            # Top right grid badge
            "drawbox=x=1360:y=60:w=500:h=60:color=0x15284F:t=fill",
            "drawtext=text='120 BPM MASTER GRID | 4/4 TIME':fontcolor=0xD42429:fontsize=22:x=1400:y=82",
            
            # Center Hero Card
            "drawbox=x=160:y=260:w=1600:h=480:color=0x122244@0.95:t=fill",
            "drawbox=x=160:y=260:w=1600:h=480:color=0xD42429:t=4",
            
            # Block ID & Title
            f"drawtext=text='[{b['id']} · {b['name'].upper()}]':fontcolor=0xD42429:fontsize=32:x=200:y=310",
            f"drawtext=textfile='{title_path_ff}':fontcolor=0xFFFFFF:fontsize=48:x=200:y=380",
            
            # Subtitle Voiceover preview (during 0s - 8s)
            f"drawtext=textfile='{sub_path_ff}':fontcolor=0xCCCCCC:fontsize=24:x=200:y=500:enable='between(t,0,8)'",
            
            # Buffer Transition notice (during 8s - 10s)
            "drawbox=x=200:y=580:w=1520:h=90:color=0xD42429@0.95:t=fill:enable='between(t,8,10)'",
            f"drawtext=textfile='{trans_path_ff}':fontcolor=0xFFFFFF:fontsize=24:x=240:y=615:enable='between(t,8,10)'",
            
            # Bottom Progress & Beat Indicator
            "drawbox=x=0:y=980:w=1920:h=100:color=0x071126:t=fill",
            "drawbox=x=0:y=980:w='1920*(t/10)':h=10:color=0xD42429:t=fill",
            f"drawtext=textfile='{meta_path_ff}':fontcolor=0x8899BB:fontsize=22:x=60:y=1020",
            
            # 120 BPM Flashing Beat Light (flashes every 0.5s)
            "drawbox=x=1800:y=1010:w=40:h=40:color=0xD42429:t=fill:enable='lt(mod(t,0.5),0.15)'"
        ]
        
        vf = ",".join(filters)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "color=c=0x0B1B3D:s=1920x1080:r=24:d=10.0",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            block_mp4
        ]
        run_cmd(cmd)
        rendered_blocks.append(block_mp4)
        
    return rendered_blocks

def assemble_master_video(work_dir, rendered_blocks, master_audio, output_mp4):
    print("=== Step 5: Stitching 60s Master Video & Embedding Audio Mix ===")
    concat_list = os.path.join(work_dir, "vid_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for rb in rendered_blocks:
            f.write(f"file '{os.path.abspath(rb).replace(os.sep, '/')}'\n")

    temp_video = os.path.join(work_dir, "raw_stitched.mp4")
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", temp_video])

    # Mux with master mixed audio
    cmd = [
        "ffmpeg", "-y",
        "-i", temp_video,
        "-i", master_audio,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        output_mp4
    ]
    run_cmd(cmd)
    print(f"\n[SUCCESS] Master Video exported to: {output_mp4}")

def main():
    start_time = time.time()
    repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    work_dir = os.path.join(repo_dir, "video_build_cache")
    os.makedirs(work_dir, exist_ok=True)
    
    output_mp4 = os.path.join(repo_dir, "deliverables", "VID-17_Motahai_60s_Master.mp4")
    os.makedirs(os.path.dirname(output_mp4), exist_ok=True)
    
    master_vo = generate_voiceovers(work_dir)
    master_music = generate_music_track(work_dir)
    master_audio = mix_master_audio(work_dir, master_vo, master_music)
    rendered_blocks = generate_video_blocks(work_dir)
    assemble_master_video(work_dir, rendered_blocks, master_audio, output_mp4)
    
    elapsed = time.time() - start_time
    print(f"Total production time: {elapsed:.2f} seconds")

if __name__ == "__main__":
    main()
