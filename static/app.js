// Phase 1: upload handler
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('file-input');
    const area = document.getElementById('upload-area');
    const status = document.getElementById('status');

    if (input) {
        input.addEventListener('change', (e) => {
            const file = e.target.files[0];
            if (file) uploadFile(file);
        });
    }

    if (area) {
        area.addEventListener('dragover', (e) => { e.preventDefault(); area.style.borderColor = '#4a90d9'; });
        area.addEventListener('dragleave', () => { area.style.borderColor = '#ccc'; });
        area.addEventListener('drop', (e) => {
            e.preventDefault();
            area.style.borderColor = '#ccc';
            const file = e.dataTransfer.files[0];
            if (file) uploadFile(file);
        });
    }

    function uploadFile(file) {
        if (!file.name.toLowerCase().endsWith('.pdf')) {
            status.innerHTML = '<p style="color:red">仅支持 PDF 文件</p>';
            return;
        }
        status.innerHTML = `<p>上传中... ${file.name} (${(file.size/1024/1024).1f} MB)</p>`;
        const fd = new FormData();
        fd.append('file', file);
        fetch('/api/jobs', { method: 'POST', body: fd })
            .then(r => r.json())
            .then(data => {
                if (data.job_id) {
                    status.innerHTML = `<p>Job 已创建: ${data.job_id}，正在处理...</p>
                        <p><a href="/jobs/${data.job_id}/review">查看进度</a></p>`;
                    // Redirect to review page after short delay
                    setTimeout(() => { window.location.href = `/jobs/${data.job_id}/review`; }, 1500);
                } else {
                    status.innerHTML = `<p style="color:red">上传失败: ${JSON.stringify(data)}</p>`;
                }
            })
            .catch(err => { status.innerHTML = `<p style="color:red">错误: ${err}</p>`; });
    }
});
