import os, shutil

root = os.path.abspath("cdk.out")
target = os.path.join(root, "asset.bb0342d5493c51c91189b5f61af3a66ce61025f729a74f6efacc744b21dd7ed7")

# Use the Windows extended-length path prefix to bypass MAX_PATH (260).
def ext(p):
    p = os.path.abspath(p)
    return "\\\\?\\" + p

if os.path.exists(ext(target)):
    print("removing corrupted asset via extended-length path ...")
    shutil.rmtree(ext(target), ignore_errors=True)
    print("removed?", not os.path.exists(ext(target)))

if os.path.exists(ext(root)):
    shutil.rmtree(ext(root), ignore_errors=True)
    print("cdk.out removed?", not os.path.exists(ext(root)))
else:
    print("cdk.out already gone")
