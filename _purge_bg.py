import os, shutil
def ext(p): return "\\\\?\\" + os.path.abspath(p)
root = "cdk.out"
while os.path.exists(ext(root)):
    shutil.rmtree(ext(root), ignore_errors=True)
open("_purge_done.txt","w").write("cdk.out removed\n")
