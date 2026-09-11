import os, shutil, sys

def ext(p):
    return "\\\\?\\" + os.path.abspath(p)

root = "cdk.out"
if os.path.exists(ext(root)):
    shutil.rmtree(ext(root), ignore_errors=True)

done = not os.path.exists(ext(root))
print("DONE cdk.out removed:", done, flush=True)
if not done:
    # list what remains at top level
    try:
        print("remaining:", os.listdir(ext(root)), flush=True)
    except Exception as e:
        print("listdir err:", e, flush=True)
