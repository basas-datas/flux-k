import os
import json
import uuid
import base64
import logging
import time
import urllib.request
import urllib.parse
import websocket
import runpod

# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== CUDA CHECK ==================

def check_cuda():
    try:
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA not available")
        logger.info("✅ CUDA available")
    except Exception as e:
        logger.error(f"❌ CUDA error: {e}")
        raise

check_cuda()

# ================== GLOBALS ==================

SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "127.0.0.1")
CLIENT_ID = str(uuid.uuid4())

COMFY_INPUT_DIR = "/workspace/ComfyUI/input"

# ================== COMFY HELPERS ==================

def wait_for_comfyui(timeout=180):
    url = f"http://{SERVER_ADDRESS}:8188/"
    for i in range(timeout):
        try:
            urllib.request.urlopen(url, timeout=3)
            logger.info("✅ ComfyUI HTTP ready")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("ComfyUI not reachable")


def save_base64_image(image_b64: str):
    os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    path = os.path.join(COMFY_INPUT_DIR, "input_image.png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(image_b64))
    logger.info(f"🖼 Saved image: {path}")


def queue_prompt(prompt):
    url = f"http://{SERVER_ADDRESS}:8188/prompt"
    payload = {"prompt": prompt, "client_id": CLIENT_ID}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id):
    url = f"http://{SERVER_ADDRESS}:8188/history/{prompt_id}"
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def get_image(filename, subfolder, folder_type):
    url = f"http://{SERVER_ADDRESS}:8188/view"
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type,
    }
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{query}") as response:
        return response.read()


def get_images(ws, workflow):
    prompt_id = queue_prompt(workflow)["prompt_id"]
    logger.info(f"🚀 prompt_id = {prompt_id}")

    # wait execution end
    while True:
        msg = ws.recv()
        if isinstance(msg, str):
            data = json.loads(msg)
            if data["type"] == "executing":
                if data["data"]["node"] is None and data["data"]["prompt_id"] == prompt_id:
                    break

    history = get_history(prompt_id)[prompt_id]
    outputs = {}

    for node_id, node_output in history["outputs"].items():
        images = []
        if "images" in node_output:
            for img in node_output["images"]:
                raw = get_image(
                    img["filename"],
                    img["subfolder"],
                    img["type"],
                )
                images.append(base64.b64encode(raw).decode("utf-8"))
        outputs[node_id] = images

    return outputs

# ================== HANDLER ==================

def handler(job):
    job_input = job.get("input", {})
    logger.info("📥 Job received")

    # 1. validate input
    image_b64 = job_input.get("image_path")
    workflow = job_input.get("workflow")

    if not image_b64:
        return {"error": "image_path is required"}
    if not workflow:
        return {"error": "workflow is required"}

    # 2. save image for LoadImage
    save_base64_image(image_b64)

    # debug check (optional but useful)
    logger.info(f"📂 ComfyUI input files: {os.listdir(COMFY_INPUT_DIR)}")

    # 3. wait ComfyUI
    wait_for_comfyui()

    # 4. websocket
    ws = websocket.WebSocket()
    ws.connect(f"ws://{SERVER_ADDRESS}:8188/ws?clientId={CLIENT_ID}")

    try:
        images = get_images(ws, workflow)
    finally:
        ws.close()

    # 5. return first image
    for node_id in images:
        if images[node_id]:
            logger.info("✅ Image generated")
            return {"image": images[node_id][0]}

    return {"error": "No image output"}

# ================== START ==================

runpod.serverless.start({"handler": handler})
