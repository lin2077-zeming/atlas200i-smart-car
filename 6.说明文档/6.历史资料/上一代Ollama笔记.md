# 上一代方案：本地模型部署与 API 接入（未在本板验证）

# 1、模型本地部署

用Ollama 部署 \(RK3576\)

## 安装 \(ARM64 原生支持\)

curl \-fsSL https://ollama\.com/install\.sh \| sh

## 拉取模型 \(选参数量最小的\)

ollama pull qwen2\.5:1\.5b   \# 1\.5B参数, 内存占用 \~1\.5GB

## 启动服务 \(后台常驻\)

ollama serve

## 验证

ollama run qwen2\.5:1\.5b

> 你好
> 
> 你好！有什么我可以帮你的吗？
> 
> 

# 2、api接入

## 3、prompt工程



