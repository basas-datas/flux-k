import copy
import os
import base64
import binascii
import json
import logging
import uuid
import urllib.request
import urllib.parse
import websocket
import runpod
from runpod.serverless.utils import rp_upload


# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ================== CUDA CHECK ==================

def check_cuda_availability():
    """Check CUDA availability and configure environment."""
    try:
        import torch
        if torch.cuda.is_available():
            logger.info("✅ CUDA is available and working")
            os.environ["CUDA_VISIBLE_DEVICES"] = "0"
            return True
        else:
            logger.error("❌ CUDA is not available")
            raise RuntimeError("CUDA is required but not available")
    except Exception as e:
        logger.error(f"❌ CUDA check failed: {e}")
        raise RuntimeError(f"CUDA initialization failed: {e}")


try:
    cuda_available = check_cuda_availability()
    if not cuda_available:
        raise RuntimeError("CUDA is not available")
except Exception as e:
    logger.error(f"Fatal error: {e}")
    logger.error("Exiting due to CUDA requirements not met")
    exit(1)


# ================== GLOBALS ==================

server_address = os.getenv("SERVER_ADDRESS", "127.0.0.1")
client_id = str(uuid.uuid4())


# ================== HELPERS ==================

def save_base64_image(data_base64: str, temp_dir: str, filename: str) -> str:
    """
    Decode a base64 image string and save it to disk.
    Returns the absolute file path.
    """
    try:
        decoded = base64.b64decode(data_base64)
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.abspath(os.path.join(temp_dir, filename))
        with open(file_path, "wb") as f:
            f.write(decoded)
        logger.info(f"Saved base64 image to: {file_path}")
        return file_path
    except (binascii.Error, ValueError) as e:
        raise ValueError("Invalid base64 image input") from e


def queue_prompt(prompt):
    url = f"http://{server_address}:8188/prompt"
    logger.info(f"Queueing prompt to: {url}")
    payload = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_image(filename, subfolder, folder_type):
    url = f"http://{server_address}:8188/view"
    logger.info(f"Fetching image from: {url}")
    params = {
        "filename": filename,
        "subfolder": subfolder,
        "type": folder_type,
    }
    query = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{query}") as response:
        return response.read()


def get_history(prompt_id):
    url = f"http://{server_address}:8188/history/{prompt_id}"
    logger.info(f"Fetching history from: {url}")
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())


def get_images(ws, prompt):
    prompt_id = queue_prompt(prompt)["prompt_id"]
    output_images = {}

    while True:
        message = ws.recv()
        if isinstance(message, str):
            data = json.loads(message)
            if data["type"] == "executing":
                if data["data"]["node"] is None and data["data"]["prompt_id"] == prompt_id:
                    break

    history = get_history(prompt_id)[prompt_id]

    for node_id, node_output in history["outputs"].items():
        images_output = []
        if "images" in node_output:
            for image in node_output["images"]:
                image_bytes = get_image(
                    image["filename"],
                    image["subfolder"],
                    image["type"],
                )
                images_output.append(
                    base64.b64encode(image_bytes).decode("utf-8")
                )
        output_images[node_id] = images_output

    return output_images


def get_workflow_from_input(job_input):
    """
    Return a deep-copied workflow provided by the client.
    No modifications are applied.
    """
    workflow_input = job_input.get("workflow")
    if workflow_input is None:
        raise ValueError("Workflow must be provided in job input")

    if isinstance(workflow_input, str):
        try:
            workflow = json.loads(workflow_input)
        except json.JSONDecodeError as e:
            raise ValueError("Invalid workflow JSON string") from e
    elif isinstance(workflow_input, dict):
        workflow = workflow_input
    else:
        raise ValueError("Workflow must be a JSON object or string")

    return copy.deepcopy(workflow)


# ================== HANDLER ==================

def handler(job):
    job_input = job.get("input", {})
    logger.info("Received job input")

    task_id = f"task_{uuid.uuid4()}"

    # Image is ALWAYS provided as base64
    image_base64 = job_input.get("image_path")
    if not image_base64:
        return {"error": "image_path (base64) is required"}

    image_path = save_base64_image(
        image_base64,
        temp_dir=task_id,
        filename="input_image.png",
    )

    try:
        workflow = get_workflow_from_input(job_input)
    except ValueError as e:
        logger.error(str(e))
        return {"error": str(e)}

    # NOTE:
    # The workflow is used AS-IS.
    # No node overrides, no parameter patching.

    ws_url = f"ws://{server_address}:8188/ws?clientId={client_id}"
    logger.info(f"Connecting to WebSocket: {ws_url}")

    # Wait until ComfyUI HTTP endpoint is ready
    http_url = f"http://{server_address}:8188/"
    for _ in range(180):
        try:
            urllib.request.urlopen(http_url, timeout=5)
            break
        except Exception:
            time.sleep(1)
    else:
        return {"error": "ComfyUI server is not reachable"}

    ws = websocket.WebSocket()
    ws.connect(ws_url)

    images = get_images(ws, workflow)
    ws.close()

    if not images:
        return {"error": "No images generated"}

    for node_id in images:
        if images[node_id]:
            return {"image": images[node_id][0]}

    return {"error": "Image output not found"}


# ================== START ==================

runpod.serverless.start({"handler": handler})
