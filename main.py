import uuid
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import uvicorn
from scraper import run_scraper, jobs

app = FastAPI(title="UGCAnalyzer Web")

# テンプレートと静的ファイルのマウント
templates = Jinja2Templates(directory="templates")
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.post("/api/analyze")
async def analyze_url(
    background_tasks: BackgroundTasks,
    base_url: str = Form(...),
    song_name: str = Form(...),
    scroll_num: int = Form(10),
    set_num: int = Form(1),
    is_headless_main: bool = Form(False)
):
    job_id = str(uuid.uuid4())
    
    # バックグラウンドタスクとしてスクレイピングを開始
    background_tasks.add_task(
        run_scraper, 
        job_id=job_id, 
        base_url=base_url, 
        song_name=song_name, 
        scroll_num=scroll_num, 
        set_num=set_num,
        is_headless_main=is_headless_main
    )
    
    return JSONResponse(content={"job_id": job_id})

@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        return JSONResponse(content={"error": "Job not found"}, status_code=404)
    
    return JSONResponse(content=jobs[job_id])

@app.get("/api/download/{job_id}")
async def download_csv(job_id: str):
    if job_id not in jobs:
        return JSONResponse(content={"error": "Job not found"}, status_code=404)
        
    job = jobs[job_id]
    if job["status"] != "completed" or not job.get("csv_path"):
        return JSONResponse(content={"error": "Job not completed or file not found"}, status_code=400)
        
    csv_path = job["csv_path"]
    if not os.path.exists(csv_path):
        return JSONResponse(content={"error": "File not found on server"}, status_code=404)
        
    filename = os.path.basename(csv_path)
    return FileResponse(path=csv_path, filename=filename, media_type='text/csv')

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
