from flask import Flask, render_template_string, request, jsonify
import sys
import io

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Python Visualizer Pro</title>
    
    <!-- Fonts & Icons -->
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- CodeMirror Core & Theme (Đảm bảo tải đúng thứ tự) -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/codemirror.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/theme/dracula.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/mode/python/python.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/addon/edit/closebrackets.min.js"></script>
    
    <style>
        :root {
            --bg-base: #090c10;
            --bg-panel: #161b22;
            --bg-header: #0d1117;
            --border-color: #30363d;
            --text-main: #c9d1d9;
            --text-muted: #8b949e;
            --accent-blue: #58a6ff;
            --accent-green: #238636;
            --accent-green-hover: #2ea043;
            --accent-purple: #a371f7;
            --accent-yellow: #e3b341;
            --accent-red: #f85149;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background-color: var(--bg-base);
            color: var(--text-main);
            font-family: 'Inter', -apple-system, sans-serif;
            display: flex;
            justify-content: center;
            padding: 30px 20px;
            min-height: 100vh;
            background-image: radial-gradient(circle at 50% 0%, rgba(88, 166, 255, 0.05) 0%, transparent 50%);
        }

        .app-container {
            width: 100%;
            max-width: 1000px;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        /* --- Top Bar / Header --- */
        .top-nav {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0 5px;
        }
        .brand {
            font-size: 18px;
            font-weight: 700;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .brand i { color: var(--accent-blue); font-size: 22px; }
        
        .btn-icon {
            background: var(--bg-panel);
            border: 1px solid var(--border-color);
            color: var(--text-main);
            width: 36px; height: 36px;
            border-radius: 8px;
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; transition: 0.2s;
        }
        .btn-icon:hover { background: #21262d; color: #fff; }

        /* --- Panel Chung --- */
        .panel {
            background: var(--bg-panel);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        .panel-header {
            background: var(--bg-header);
            padding: 10px 16px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
        }
        
        /* --- AI Prompt Bar --- */
        .ai-prompt-bar {
            display: flex;
            padding: 12px;
            background: #1c2128;
            border-bottom: 1px solid var(--border-color);
            gap: 10px;
        }
        .ai-prompt-bar input {
            flex: 1;
            background: var(--bg-header);
            border: 1px solid var(--border-color);
            color: #fff;
            padding: 10px 15px;
            border-radius: 6px;
            font-family: 'Inter', sans-serif;
            font-size: 14px;
            outline: none;
            transition: 0.2s;
        }
        .ai-prompt-bar input:focus { border-color: var(--accent-purple); box-shadow: 0 0 0 2px rgba(163, 113, 247, 0.2); }
        
        /* --- Code Editor (CodeMirror Override) --- */
        .CodeMirror {
            height: 320px !important;
            font-family: 'Fira Code', monospace !important;
            font-size: 15px !important;
            background: var(--bg-panel) !important;
            color: var(--text-main) !important;
        }
        .CodeMirror-gutters {
            background: var(--bg-panel) !important;
            border-right: 1px solid var(--border-color) !important;
            padding-right: 5px;
        }
        .CodeMirror-linenumber { color: #484f58 !important; }
        .CodeMirror-cursor { border-left: 2px solid var(--accent-blue) !important; }
        
        /* Hiệu ứng Highlight dòng đang chạy */
        .CodeMirror-activeline-background {
            background: linear-gradient(90deg, rgba(88, 166, 255, 0.1) 0%, transparent 100%) !important;
            border-left: 3px solid var(--accent-blue);
        }
        .dimmed-line { opacity: 0.3; transition: opacity 0.3s; }

        /* --- Variables (Biến) --- */
        .vars-container {
            padding: 16px;
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            background: var(--bg-header);
            border-top: 1px solid var(--border-color);
            min-height: 80px;
            align-items: center;
        }
        .var-card {
            background: var(--bg-panel);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 10px 20px;
            display: flex; flex-direction: column; align-items: center;
            min-width: 120px;
            position: relative;
            animation: popIn 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275) forwards;
        }
        .var-card::before {
            content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 2px;
            background: linear-gradient(90deg, var(--accent-blue), var(--accent-purple));
            border-radius: 8px 8px 0 0;
        }
        @keyframes popIn { from { transform: scale(0.8); opacity: 0; } to { transform: scale(1); opacity: 1; } }
        .var-name { font-size: 12px; color: var(--accent-blue); margin-bottom: 4px; font-weight: 600; }
        .var-value { font-size: 16px; color: var(--accent-yellow); font-family: 'Fira Code', monospace; font-weight: 700; }

        /* --- Console / Output --- */
        .console-output {
            padding: 20px;
            font-family: 'Fira Code', monospace;
            font-size: 15px;
            color: #a5d6ff;
            background: #010409;
            min-height: 150px;
            max-height: 300px;
            overflow-y: auto;
            white-space: pre-wrap;
            line-height: 1.5;
            position: relative;
        }
        .console-output::after {
            content: '▋'; animation: blink 1s step-end infinite; color: var(--text-muted);
        }
        @keyframes blink { 50% { opacity: 0; } }
        .error-text { color: var(--accent-red); }

        /* --- AI Explain Box --- */
        .ai-explain-box {
            margin: 0 20px 20px 20px;
            padding: 15px;
            background: rgba(163, 113, 247, 0.1);
            border: 1px solid rgba(163, 113, 247, 0.3);
            border-radius: 8px;
            font-size: 14px;
            line-height: 1.6;
            color: #e2e8f0;
            display: none;
        }
        .ai-explain-box strong { color: var(--accent-purple); }

        /* --- Buttons --- */
        .btn {
            display: inline-flex; align-items: center; justify-content: center; gap: 6px;
            padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 600;
            cursor: pointer; border: 1px solid transparent; transition: 0.2s;
            font-family: 'Inter', sans-serif;
        }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        
        .btn-primary { background: var(--accent-green); color: #fff; border-color: rgba(240,246,252,0.1); }
        .btn-primary:hover:not(:disabled) { background: var(--accent-green-hover); }
        
        .btn-secondary { background: #21262d; color: var(--text-main); border-color: var(--border-color); }
        .btn-secondary:hover:not(:disabled) { background: #30363d; color: #fff; }

        .btn-ai { background: linear-gradient(135deg, rgba(163, 113, 247, 0.2), rgba(88, 166, 255, 0.2)); color: #d2a8ff; border-color: rgba(163, 113, 247, 0.3); }
        .btn-ai:hover:not(:disabled) { background: linear-gradient(135deg, rgba(163, 113, 247, 0.3), rgba(88, 166, 255, 0.3)); }
        
        .btn-danger { background: rgba(248, 81, 73, 0.1); color: var(--accent-red); border-color: rgba(248, 81, 73, 0.3); }
        .btn-danger:hover:not(:disabled) { background: rgba(248, 81, 73, 0.2); }

        /* --- Modal API Key --- */
        .modal-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.7); backdrop-filter: blur(4px);
            display: none; justify-content: center; align-items: center; z-index: 100;
        }
        .modal-content {
            background: var(--bg-panel); border: 1px solid var(--border-color);
            border-radius: 12px; width: 400px; padding: 24px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        .modal-header { font-size: 18px; font-weight: 600; margin-bottom: 15px; color: #fff; }
        .modal-desc { font-size: 13px; color: var(--text-muted); margin-bottom: 15px; line-height: 1.5; }
        .modal-desc a { color: var(--accent-blue); text-decoration: none; }
        .input-group { margin-bottom: 20px; }
        .input-group input {
            width: 100%; padding: 10px 12px; background: var(--bg-header);
            border: 1px solid var(--border-color); border-radius: 6px;
            color: #fff; font-family: monospace; outline: none;
        }
        .input-group input:focus { border-color: var(--accent-blue); }
        
        /* Spinner */
        .spinner { display: none; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 0.8s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
    </style>
</head>
<body>

<!-- Modal Cài đặt API Key -->
<div class="modal-overlay" id="api-modal">
    <div class="modal-content">
        <div class="modal-header"><i class="fa-solid fa-key"></i> Cấu hình Gemini API</div>
        <div class="modal-desc">
            Nhập API Key của bạn để sử dụng các tính năng AI. Lấy key miễn phí tại 
            <a href="https://aistudio.google.com/apikey" target="_blank">Google AI Studio <i class="fa-solid fa-external-link-alt" style="font-size:10px;"></i></a>.
        </div>
        <div class="input-group">
            <input type="password" id="api-key-input" placeholder="AIzaSy...">
        </div>
        <div style="display: flex; justify-content: flex-end; gap: 10px;">
            <button class="btn btn-secondary" onclick="closeModal()">Hủy</button>
            <button class="btn btn-primary" onclick="saveApiKey()">Lưu Key</button>
        </div>
    </div>
</div>

<div class="app-container">
    <!-- Top Nav -->
    <div class="top-nav">
        <div class="brand">
            <i class="fa-brands fa-python"></i> Python Visualizer <span style="font-size: 12px; background: var(--accent-purple); color: #fff; padding: 2px 6px; border-radius: 4px; margin-left: 8px;">AI</span>
        </div>
        <button class="btn-icon" onclick="openModal()" title="Cài đặt API Key"><i class="fa-solid fa-gear"></i></button>
    </div>

    <!-- Khu vực Code -->
    <div class="panel">
        <div class="panel-header">
            <span><i class="fa-regular fa-file-code" style="margin-right: 6px;"></i>main.py</span>
            <div style="display: flex; gap: 8px;">
                <button class="btn btn-danger" id="btn-reset" onclick="resetExecution()" style="display: none;"><i class="fa-solid fa-rotate-left"></i> Làm mới</button>
                <button class="btn btn-secondary" id="btn-step" onclick="startExecution(true)"><i class="fa-solid fa-forward-step"></i> Từng bước</button>
                <button class="btn btn-primary" id="btn-run" onclick="startExecution(false)"><i class="fa-solid fa-play"></i> Chạy (Ctrl+Enter)</button>
            </div>
        </div>
        
        <!-- Thanh Prompt AI -->
        <div class="ai-prompt-bar">
            <input type="text" id="ai-prompt" placeholder="Nhập yêu cầu để AI viết code (VD: Tính tổng từ 1 đến 10)..." onkeypress="handlePromptEnter(event)">
            <button class="btn btn-ai" id="btn-generate" onclick="generateCode()">
                <div class="spinner" id="gen-spinner"></div>
                <i class="fa-solid fa-wand-magic-sparkles" id="gen-icon"></i> Tạo Code
            </button>
        </div>

        <textarea id="code-editor"></textarea>
        <div id="vars-container" class="vars-container"></div>
    </div>

    <!-- Khu vực Kết quả -->
    <div class="panel">
        <div class="panel-header">
            <span><i class="fa-solid fa-terminal" style="margin-right: 6px;"></i>Console Output</span>
            <button class="btn btn-ai" id="btn-explain" onclick="explainError()" style="display: none;"><i class="fa-solid fa-robot"></i> Hỏi AI về lỗi này</button>
        </div>
        <div class="console-output" id="output"></div>
        <div id="ai-explain-box" class="ai-explain-box"></div>
    </div>
</div>

<script>
    // ================= KHỞI TẠO CODEMIRROR =================
    var editor = CodeMirror.fromTextArea(document.getElementById("code-editor"), {
        mode: "python",
        theme: "dracula",
        lineNumbers: true,
        autoCloseBrackets: true,
        indentUnit: 4,
        styleActiveLine: true
    });

    editor.setValue('name = "hancontam"\\nage = 18\\nprint(name)\\nprint(age)');

    // ================= BIẾN TOÀN CỤC =================
    let executionSteps = [];
    let currentStepIndex = 0;
    let playTimeout = null;
    let previousVars = {};
    let currentError = "";

    // Phím tắt Ctrl + Enter để chạy
    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            startExecution(false);
        }
    });

    // ================= QUẢN LÝ API KEY =================
    window.onload = () => {
        const savedKey = localStorage.getItem("gemini_api_key");
        if (savedKey) document.getElementById("api-key-input").value = savedKey;
    };

    function openModal() { document.getElementById("api-modal").style.display = "flex"; }
    function closeModal() { document.getElementById("api-modal").style.display = "none"; }
    
    function saveApiKey() {
        const key = document.getElementById("api-key-input").value.trim();
        if (key) {
            localStorage.setItem("gemini_api_key", key);
            closeModal();
        } else {
            alert("Vui lòng nhập API Key.");
        }
    }

    function getApiKey() {
        const key = document.getElementById("api-key-input").value.trim() || localStorage.getItem("gemini_api_key");
        if (!key) {
            openModal();
            return null;
        }
        return key;
    }

    // ================= GỌI GEMINI API =================
    async function callGeminiAPI(prompt) {
        const apiKey = getApiKey();
        if (!apiKey) return null;

        // Sử dụng model flash mới nhất (2.5-flash hoặc 1.5-flash tùy thời điểm)
        const model = "gemini-2.5-flash"; 
        const url = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;

        try {
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "x-goog-api-key": apiKey
                },
                body: JSON.stringify({
                    contents: [{ parts: [{ text: prompt }] }]
                })
            });

            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error?.message || `HTTP ${response.status}`);
            }
            return data.candidates[0].content.parts[0].text;
        } catch (error) {
            console.error("Gemini API Error:", error);
            alert("Lỗi khi gọi AI: " + error.message);
            return null;
        }
    }

    // ================= TÍNH NĂNG PROMPT TO CODE =================
    function handlePromptEnter(e) {
        if (e.key === 'Enter') generateCode();
    }

    async function generateCode() {
        const promptText = document.getElementById("ai-prompt").value.trim();
        if (!promptText) return;

        const btn = document.getElementById("btn-generate");
        const spinner = document.getElementById("gen-spinner");
        const icon = document.getElementById("gen-icon");

        btn.disabled = true;
        spinner.style.display = "block";
        icon.style.display = "none";

        const prompt = `You are a Python expert. Write ONLY valid Python code for the following request. Do NOT include markdown formatting, explanations, or backticks. Code: ${promptText}`;
        let generatedCode = await callGeminiAPI(prompt);

        if (generatedCode) {
            generatedCode = generatedCode.replace(/^```python\n/, '').replace(/^```\n/, '').replace(/\n```$/, '');
            editor.setValue(generatedCode);
            resetExecution();
        }

        btn.disabled = false;
        spinner.style.display = "none";
        icon.style.display = "block";
    }

    // ================= TÍNH NĂNG AI GIẢI THÍCH LỖI =================
    async function explainError() {
        if (!currentError) return;
        const code = editor.getValue();
        
        const btn = document.getElementById("btn-explain");
        const explainBox = document.getElementById("ai-explain-box");
        
        btn.innerHTML = '<div class="spinner" style="display:block; width:12px; height:12px; border-width:2px;"></div> Đang phân tích...';
        btn.disabled = true;
        explainBox.style.display = "block";
        explainBox.innerHTML = "<em>AI đang phân tích lỗi...</em>";

        const prompt = `The user ran this Python code:\\n\\`\\`\\`python\\n${code}\\n\\`\\`\\`\\nIt produced this error:\\n\\`\\`\\`\\n${currentError}\\n\\`\\`\\`\\nExplain the error in Vietnamese simply and suggest how to fix it. Use plain text, no markdown code blocks for the explanation.`;
        
        const response = await callGeminiAPI(prompt);

        if (response) {
            explainBox.innerHTML = `<strong><i class="fa-solid fa-robot"></i> AI Giải thích:</strong><br><br>${response.replace(/\\n/g, '<br>')}`;
        } else {
            explainBox.innerHTML = "<span style='color:red'>Không thể lấy phản hồi từ AI.</span>";
        }

        btn.innerHTML = '<i class="fa-solid fa-robot"></i> Hỏi AI về lỗi này';
        btn.disabled = false;
    }

    // ================= LOGIC THỰC THI CODE =================
    function resetExecution() {
        clearTimeout(playTimeout);
        
        document.getElementById('btn-run').disabled = false;
        document.getElementById('btn-step').disabled = false;
        document.getElementById('btn-reset').style.display = 'none';
        document.getElementById('btn-explain').style.display = 'none';
        
        document.getElementById('btn-step').innerHTML = '<i class="fa-solid fa-forward-step"></i> Từng bước';
        document.getElementById('btn-step').onclick = () => startExecution(true);
        
        const totalLines = editor.lineCount();
        for (let i = 0; i < totalLines; i++) {
            editor.removeLineClass(i, 'wrap', 'dimmed-line');
            editor.removeLineClass(i, 'wrap', 'CodeMirror-activeline-background');
        }
        
        document.getElementById('vars-container').innerHTML = '';
        document.getElementById('output').textContent = '';
        document.getElementById('output').classList.remove('error-text');
        document.getElementById('ai-explain-box').style.display = 'none';
        previousVars = {};
        currentError = "";
    }

    async function startExecution(isStepMode = false) {
        resetExecution();
        document.getElementById('btn-run').disabled = true;
        document.getElementById('btn-step').disabled = true;

        const code = editor.getValue();
        const totalLines = editor.lineCount();
        
        for (let i = 0; i < totalLines; i++) {
            editor.addLineClass(i, 'wrap', 'dimmed-line');
        }

        try {
            const response = await fetch('/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code })
            });
            const data = await response.json();
            executionSteps = data.steps;
            currentStepIndex = 0;

            if (isStepMode) {
                document.getElementById('btn-step').disabled = false;
                document.getElementById('btn-step').innerHTML = '<i class="fa-solid fa-arrow-right"></i> Bước tiếp';
                document.getElementById('btn-step').onclick = playNextStep;
                playNextStep();
            } else {
                playAuto();
            }
        } catch (error) {
            document.getElementById('output').textContent = "Lỗi kết nối tới server.";
            document.getElementById('output').classList.add('error-text');
            resetExecution();
        }
    }

    function renderStep(step) {
        if (step.line !== -1) {
            const lineIdx = step.line - 1;
            editor.removeLineClass(lineIdx, 'wrap', 'dimmed-line');
            if (currentStepIndex > 0 && executionSteps[currentStepIndex-1].line !== -1) {
                editor.removeLineClass(executionSteps[currentStepIndex-1].line - 1, 'wrap', 'CodeMirror-activeline-background');
            }
            editor.addLineClass(lineIdx, 'wrap', 'CodeMirror-activeline-background');
            editor.scrollIntoView({line: lineIdx, ch: 0}, 200);
        }

        const varsContainer = document.getElementById('vars-container');
        Array.from(varsContainer.children).forEach(child => {
            if (!(child.dataset.name in step.vars)) child.remove();
        });

        for (const [key, value] of Object.entries(step.vars)) {
            let block = varsContainer.querySelector(`[data-name="${key}"]`);
            const displayValue = String(value);
            if (!block) {
                block = document.createElement('div');
                block.className = 'var-card';
                block.dataset.name = key;
                block.innerHTML = `<div class="var-name">${key}</div><div class="var-value">${displayValue}</div>`;
                varsContainer.appendChild(block);
                previousVars[key] = displayValue;
            } else if (previousVars[key] !== displayValue) {
                block.querySelector('.var-value').textContent = displayValue;
                previousVars[key] = displayValue;
            }
        }

        const outputElement = document.getElementById('output');
        outputElement.textContent = step.stdout;
        if (step.error) {
            outputElement.textContent += '\\n' + step.error;
            outputElement.classList.add('error-text');
            currentError = step.error;
        }
    }

    function playNextStep() {
        if (currentStepIndex >= executionSteps.length) {
            finishExecution();
            return;
        }
        renderStep(executionSteps[currentStepIndex]);
        currentStepIndex++;
    }

    function playAuto() {
        if (currentStepIndex >= executionSteps.length) {
            finishExecution();
            return;
        }
        renderStep(executionSteps[currentStepIndex]);
        currentStepIndex++;
        playTimeout = setTimeout(playAuto, 800); 
    }

    function finishExecution() {
        if (executionSteps.length > 0 && executionSteps[executionSteps.length-1].line !== -1) {
            editor.removeLineClass(executionSteps[executionSteps.length-1].line - 1, 'wrap', 'CodeMirror-activeline-background');
        }
        
        document.getElementById('btn-run').disabled = false;
        document.getElementById('btn-step').disabled = true;
        document.getElementById('btn-reset').style.display = 'flex';
        
        if (currentError) {
            document.getElementById('btn-explain').style.display = 'flex';
        }
    }
</script>

</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/run', methods=['POST'])
def run_code():
    data = request.get_json()
    code = data.get('code', '')

    steps = []
    stdout_capture = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = stdout_capture

    def tracer(frame, event, arg):
        if event == 'line' and frame.f_code.co_filename == '<string>':
            vars_dict = {}
            for k, v in frame.f_locals.items():
                if k.startswith('__'): continue
                if isinstance(v, (int, float, str, bool)):
                    vars_dict[k] = v
            for k, v in frame.f_globals.items():
                if k.startswith('__'): continue
                if isinstance(v, (int, float, str, bool)) and k not in vars_dict:
                    vars_dict[k] = v

            steps.append({
                'line': frame.f_lineno,
                'vars': vars_dict,
                'stdout': stdout_capture.getvalue()
            })
        return tracer

    error_msg = None
    try:
        sys.settrace(tracer)
        compiled = compile(code, '<string>', 'exec')
        exec(compiled, {})
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
    finally:
        sys.settrace(None)
        sys.stdout = old_stdout

    final_vars = steps[-1]['vars'] if steps else {}
    steps.append({
        'line': -1,
        'vars': final_vars,
        'stdout': stdout_capture.getvalue(),
        'error': error_msg
    })

    return jsonify({'steps': steps})

if __name__ == '__main__':
    print("Đang khởi động server...")
    print("Mở trình duyệt và truy cập: http://127.0.0.1:5000")
    app.run(debug=True, port=5000)