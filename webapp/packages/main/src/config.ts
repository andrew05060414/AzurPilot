const yaml = require('yaml');
const fs = require('fs');
const path = require('path');
const {cwd, env} = require('process');

function findAlasPath() {
    const candidates = [
        env.PORTABLE_EXECUTABLE_DIR,
        env.INIT_CWD,
        cwd(),
        path.resolve(__dirname, '../../../..'),
        path.resolve(__dirname, '../../..'),
    ].filter((candidate): candidate is string => Boolean(candidate));

    for (const candidate of candidates) {
        let current = path.resolve(candidate);
        for (let i = 0; i < 6; i++) {
            if (fs.existsSync(path.join(current, 'config', 'deploy.yaml'))) {
                return current;
            }
            const parent = path.dirname(current);
            if (parent === current) break;
            current = parent;
        }
    }

    return path.resolve(env.PORTABLE_EXECUTABLE_DIR || cwd());
}

// export const alasPath = 'D:/AzurLaneAutoScript';
export const alasPath = findAlasPath();

const file = fs.readFileSync(path.join(alasPath, './config/deploy.yaml'), 'utf8');
const config = yaml.parse(file);
const PythonExecutable = config.Deploy.Python.PythonExecutable;
const WebuiPort = config.Deploy.Webui.WebuiPort.toString();

export const pythonPath = (path.isAbsolute(PythonExecutable) ? PythonExecutable : path.join(alasPath, PythonExecutable));
export const webuiUrl = `http://127.0.0.1:${WebuiPort}`;
export const webuiPath = 'gui.py';
export const webuiArgs = ['--port', WebuiPort, '--electron'];
export const dpiScaling = Boolean(config.Deploy.Webui.DpiScaling) || (config.Deploy.Webui.DpiScaling === undefined) ;
