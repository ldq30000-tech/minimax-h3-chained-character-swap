import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const NODE_NAME = "H3FinalTrimToSource";
const MIN_WIDTH = 620;
const MIN_HEIGHT = 690;

function nodeType(node) {
    return node?.comfyClass ?? node?.type ?? "";
}

function videoUrl(item) {
    const query = new URLSearchParams({
        filename: item.filename,
        subfolder: item.subfolder ?? "",
        type: item.type ?? "output",
    });
    return api.apiURL(`/view?${query.toString()}`);
}

function executionTimestamp(entry) {
    for (const message of entry?.status?.messages ?? []) {
        if (message?.[0] === "execution_start") {
            return Number(message?.[1]?.timestamp) || 0;
        }
    }
    return 0;
}

async function restoreLatestPreview(node) {
    try {
        const response = await api.fetchApi("/history?max_items=50");
        if (response.ok) {
            const history = await response.json();
            let latest = null;
            let latestTimestamp = -1;
            for (const entry of Object.values(history ?? {})) {
                const output = entry?.outputs?.[String(node.id)];
                if (!output?.videos?.length) continue;
                const timestamp = executionTimestamp(entry);
                if (timestamp >= latestTimestamp) {
                    latest = output;
                    latestTimestamp = timestamp;
                }
            }
            if (latest) {
                node._h3FinalPreviewShow?.(latest);
                return;
            }
        }

        const filenamePrefix = node.widgets?.find((widget) => widget.name === "filename")?.value ?? "";
        const query = new URLSearchParams({ filename_prefix: String(filenamePrefix) });
        const latestResponse = await api.fetchApi(
            `/h3-chained-character-swap/latest-final?${query.toString()}`,
        );
        if (!latestResponse.ok) return;
        const latest = await latestResponse.json();
        if (latest?.video) {
            node._h3FinalPreviewShow?.({
                text: [latest.status],
                videos: [latest.video],
            });
        }
    } catch (error) {
        console.warn("[H3 Final Preview] Could not restore history preview", error);
    }
}

function mount(node) {
    if (node._h3FinalPreviewMounted || typeof node.addDOMWidget !== "function") return;
    node._h3FinalPreviewMounted = true;

    const root = document.createElement("div");
    root.style.cssText = [
        "box-sizing:border-box",
        "display:flex",
        "flex-direction:column",
        "gap:8px",
        "width:100%",
        "height:100%",
        "padding:8px",
        "overflow:hidden",
        "background:#101214",
        "color:#e8eaed",
        "font:13px/1.4 system-ui,sans-serif",
    ].join(";");

    for (const eventName of [
        "pointerdown", "pointerup", "mousedown", "mouseup", "click", "dblclick", "wheel",
    ]) {
        root.addEventListener(eventName, (event) => event.stopPropagation());
    }

    const header = document.createElement("div");
    header.textContent = "最终完整视频";
    header.style.cssText = "font-weight:600;color:#f4f6f8";

    const video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.style.cssText = [
        "display:none",
        "width:100%",
        "height:520px",
        "object-fit:contain",
        "background:#000",
        "border:1px solid #34383d",
        "border-radius:6px",
    ].join(";");

    const status = document.createElement("div");
    status.textContent = "等待生成，或正在查找最近一次成片...";
    status.style.cssText = "min-height:38px;color:#b8bec6;white-space:normal;overflow-wrap:anywhere";

    const openLink = document.createElement("a");
    openLink.textContent = "在新窗口打开完整视频";
    openLink.target = "_blank";
    openLink.rel = "noopener";
    openLink.style.cssText = "display:none;color:#78b7ff;text-decoration:none";

    root.append(header, video, status, openLink);

    node._h3FinalPreviewShow = (message) => {
        const item = message?.videos?.[0] ?? message?.gifs?.[0];
        const text = Array.isArray(message?.text) ? message.text[0] : message?.text;
        if (text) status.textContent = text;
        if (!item?.filename) return;
        const url = videoUrl(item);
        video.style.display = "block";
        video.src = url;
        video.load();
        openLink.href = url;
        openLink.style.display = "inline-block";
        node.setSize?.([
            Math.max(Number(node.size?.[0]) || 0, MIN_WIDTH),
            Math.max(Number(node.size?.[1]) || 0, MIN_HEIGHT),
        ]);
        node.graph?.setDirtyCanvas?.(true, true);
    };

    video.addEventListener("error", () => {
        status.textContent = "成片文件存在，但浏览器预览加载失败。请点击下方链接打开。";
    });

    const widget = node.addDOMWidget("h3_final_video", "h3-final-video", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 600,
    });
    widget.serialize = false;
    node.setSize?.([
        Math.max(Number(node.size?.[0]) || 0, MIN_WIDTH),
        Math.max(Number(node.size?.[1]) || 0, MIN_HEIGHT),
    ]);

    const removed = node.onRemoved;
    node.onRemoved = function () {
        video.pause();
        video.removeAttribute("src");
        video.load();
        return removed?.apply(this, arguments);
    };

    const queued = node._h3FinalPreviewQueued;
    delete node._h3FinalPreviewQueued;
    if (queued) node._h3FinalPreviewShow(queued);
    setTimeout(() => restoreLatestPreview(node), 0);
}

app.registerExtension({
    name: "minimax_h3_chained_character_swap.final_video_preview",
    async beforeRegisterNodeDef(nodeTypeDefinition, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const created = nodeTypeDefinition.prototype.onNodeCreated;
        nodeTypeDefinition.prototype.onNodeCreated = function () {
            const result = created?.apply(this, arguments);
            setTimeout(() => mount(this), 0);
            return result;
        };

        const executed = nodeTypeDefinition.prototype.onExecuted;
        nodeTypeDefinition.prototype.onExecuted = function (message) {
            const result = executed?.apply(this, arguments);
            if (this._h3FinalPreviewShow) this._h3FinalPreviewShow(message);
            else this._h3FinalPreviewQueued = message;
            return result;
        };
    },
    async nodeCreated(node) {
        if (nodeType(node) === NODE_NAME) mount(node);
    },
    async afterConfigureGraph() {
        for (const node of app.graph?._nodes ?? []) {
            if (nodeType(node) === NODE_NAME) {
                mount(node);
                restoreLatestPreview(node);
            }
        }
    },
});
