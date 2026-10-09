import os
import sys
import subprocess
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
        "img": "b01_pressure.jpg",
        "title": "WORK MOVES FAST.",
        "sub": "Your business moves fast. Your workforce should move faster--without adding headcount or drag.",
        "zoom_dir": "in"
    },
    {
        "id": "B02",
        "name": "Reveal",
        "img": "b02_reveal.jpg",
        "title": "ONE OBJECTIVE. -> AUTONOMOUS EXECUTION.",
        "sub": "Meet Motahai: an autonomous AI workforce that executes across your business around the clock.",
        "zoom_dir": "in"
    },
    {
        "id": "B03",
        "name": "Orchestrate",
        "img": "b03_orchestrate.jpg",
        "title": "COORDINATED BY DESIGN.",
        "sub": "Every agent collaborates, checks context, and hands off intelligently with full operational visibility.",
        "zoom_dir": "in"
    },
    {
        "id": "B04",
        "name": "Execute",
        "img": "b04_execute.jpg",
        "title": "RESEARCH * OPERATIONS * MARKETING * ANALYTICS",
        "sub": "From research and operations to marketing and analytics, Motahai transforms processes into dependable execution.",
        "zoom_dir": "in"
    },
    {
        "id": "B05",
        "name": "Govern",
        "img": "b05_govern.jpg",
        "title": "CONTROL WITHOUT BOTTLENECKS.",
        "sub": "You stay in command with clear approvals and traceable decisions while agents handle the load.",
        "zoom_dir": "in"
    },
    {
        "id": "B06",
        "name": "Outcome",
        "img": "b06_outcome.jpg",
        "title": "MULTIPLY YOUR CAPACITY. -> MOTAHAI",
        "sub": "Move from scattered tasks to an AI-native operation. Deploy Motahai and let your business run ahead.",
        "zoom_dir": "in"
    }
]

def render_cinematic_clips(assets_dir, work_dir):
    print("=== Step 1: Rendering Cinematic Camera Moves (Ken Burns 24fps) ===")
    rendered_clips = []
    
    for idx, b in enumerate(BLOCKS):
        img_path = os.path.join(assets_dir, b["img"]).replace("\\", "/")
        out_mp4 = os.path.join(work_dir, f"cinematic_{b['id']}.mp4").replace("\\", "/")
        
        # Subtitle file
        sub_file = os.path.join(work_dir, f"sub_cinematic_{b['id']}.txt")
        with open(sub_file, "w", encoding="utf-8") as f:
            f.write(f"{b['title']}  |  {b['sub']}")
        sub_file_ff = sub_file.replace("\\", "/").replace(":", "\\:")

        # Zoom in filter over 240 frames (10 seconds at 24fps)
        # Using scale and zoompan
        vf_filters = [
            "scale=3840:2160",
            "zoompan=z='min(zoom+0.0008,1.20)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=240:s=1920x1080:fps=24",
            # Brand Header Badge in Safe Zone
            "drawbox=x=60:y=60:w=480:h=50:color=0x0B1B3D@0.85:t=fill",
            "drawbox=x=60:y=60:w=480:h=50:color=0xD42429:t=2",
            "drawtext=text='MOTAHAI  |  AI WORKFORCE':fontcolor=0xFFFFFF:fontsize=20:x=85:y=76",
            # Bottom Subtitle Gradient Bar
            "drawbox=x=0:y=920:w=1920:h=160:color=0x071126@0.85:t=fill",
            "drawbox=x=0:y=920:w='1920*(t/10)':h=4:color=0xD42429:t=fill",
            # Subtitle text
            f"drawtext=textfile='{sub_file_ff}':fontcolor=0xFFFFFF:fontsize=26:x=80:y=970:enable='between(t,0,8)'",
            # Transition notice
            f"drawtext=text='>>> [2s TRANSITION BUFFER  *  120 BPM BEAT SHIFT] <<<':fontcolor=0xD42429:fontsize=22:x=80:y=970:enable='between(t,8,10)'"
        ]
        
        vf = ",".join(vf_filters)
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", img_path,
            "-vf", vf,
            "-t", "10.0",
            "-c:v", "libx264",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            out_mp4
        ]
        run_cmd(cmd)
        rendered_clips.append(out_mp4)
        print(f"[OK] Rendered Block {b['id']} ({b['name']})")
        
    return rendered_clips

def assemble_cinematic_master(work_dir, rendered_clips, master_audio, output_mp4):
    print("=== Step 2: Stitching Master Timeline & Syncing Audio Mix ===")
    concat_list = os.path.join(work_dir, "cinematic_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for rc in rendered_clips:
            f.write(f"file '{os.path.abspath(rc).replace(os.sep, '/')}'\n")

    temp_video = os.path.join(work_dir, "cinematic_stitched.mp4")
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", temp_video])

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
    print(f"\n[SUCCESS] Cinematic AI Master Video generated at: {output_mp4}")

def main():
    start_time = time.time()
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.path.join(repo_dir, "assets", "cinematic_scenes")
    work_dir = os.path.join(repo_dir, "video_build_cache")
    master_audio = os.path.join(work_dir, "master_mix.wav")
    output_mp4 = os.path.join(repo_dir, "deliverables", "VID-19_Motahai_60s_Cinematic_AI_Master.mp4")
    
    rendered_clips = render_cinematic_clips(assets_dir, work_dir)
    assemble_cinematic_master(work_dir, rendered_clips, master_audio, output_mp4)
    
    elapsed = time.time() - start_time
    print(f"Total production time: {elapsed:.2f} seconds")

if __name__ == "__main__":
    main()
