import os
import subprocess

if __name__ == "__main__":
    print("🔁 Cloning latest repo version (if needed)...")
    repo_url = os.getenv("REPO_URL", "https://github.com/basas-datas/flux-k")
    clone_dir = "/workspace/app"

    if not os.path.exists(clone_dir):
        subprocess.run(["git", "clone", repo_url, clone_dir], check=True)

    os.chdir(clone_dir)
    print("🚀 Starting entrypoint.sh...")
    subprocess.run(["bash", "/workspace/app/entrypoint.sh"])
