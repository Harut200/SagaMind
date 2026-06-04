# SagaMind

[![GitHub license](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/your-username/sagamind/blob/main/LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11-green.svg)](https://python.org)
[![gRPC](https://img.shields.io/badge/gRPC-v1.54-orange.svg)](https://grpc.io)
[![Z3 Solver](https://img.shields.io/badge/Z3%20SMT-v4.12-blueviolet.svg)](https://github.com/Z3Prover/z3)
[![WebAssembly](https://img.shields.io/badge/WebAssembly-Wasmtime-red.svg)](https://wasmtime.dev)

> **SagaMind** is a high-performance, transaction-safe multi-agent runtime and tiered memory co-processor. It bridges the gap between *probabilistic* LLM reasoning and *deterministic* software engineering reliability.

---

## Why SagaMind?

Deploying multi-agent networks in production software environments is currently bottlenecked by two major failures:
1.  **State Corruption (Brittle execution):** When an agent executes a sequence of API, file, or database commands and fails at step 8, the environment is left corrupted.
2.  **Context Bloat (Goldfish Memory):** Agents carry flat vector buffers of conversation logs, leading to context-window pollution, high costs, and hallucinations.

SagaMind resolves this by introducing an **Agentic Saga Transaction Protocol**, a **Neuro-Symbolic Z3 safety gate**, and a biologically-inspired **tiered memory consolidation engine (sleep cycle)**.

```
                  ┌─────────────────────────────────────┐
                  │           SagaMind Engine           │
                  └──────────────────┬──────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         ▼                           ▼                           ▼
┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│   Transaction   │         │  Tiered Memory  │         │ Neuro-Symbolic  │
│  Orchestrator   │         │  Co-Processor   │         │    Verifier     │
│ (Saga Pattern)  │         │ (CLS & Sleep)   │         │ (Z3 SMT Solver) │
└─────────────────┘         └─────────────────┘         └─────────────────┘
```

---

## Features

*   **Transactional Saga Execution:** Treat agent actions as local transactions with stateful, LIFO-ordered compensating rollbacks to clean the environment when failures occur.
*   **Tiered Memory Consolidation:** Active memory decay using Hermann Ebbinghaus forgetting curves. A background "sleep cycle" DBSCAN-clusters episodic logs and distills them into a Neo4j semantic concept graph.
*   **Neuro-Symbolic Safety Gate:** Automatically parses agent proposals and proves safety properties using the Z3 SMT solver before executing code on your host.
*   **Speculative Tool Execution:** Speeds up execution by up to 60% by running predicted tool parameters in parallel inside temporary, isolated Copy-on-Write (COW) WebAssembly sandboxes.

---

## Comparison with Existing Frameworks

| Feature | LangGraph / CrewAI | Mem0 / Cognee | **SagaMind** |
| :--- | :--- | :--- | :--- |
| **Transaction Safety** | (DAGs only, no rollback) | | **Yes (Saga Compensations)** |
| **Active Memory Decay**| (Linear context growth) | (Static retrieval) | **Yes (Ebbinghaus Math)** |
| **Graph Consolidation**| | (No sleep cycle) | **Yes (DBSCAN Sleep Distillation)**|
| **Formal Invariants**  | | | **Yes (Z3 SMT Invariant Proving)** |
| **Speculative Run**    | (Sequential) | | **Yes (Parallel COW Sandboxing)** |

---

## Quick Start

### 1. Prerequisite Setup
Ensure you have Python 3.9+ and optionally the Z3 solver binary installed on your system.

```bash
# Clone the repository
git clone https://github.com/your-username/sagamind.git
cd sagamind

# Install python dependencies
pip install -r requirements.txt
```

### 2. Launch the Interactive Dashboard Demo
SagaMind includes a beautiful, premium visual dashboard showing the live transaction rollback flows, memory decay values, and Z3 symbolic verification logs.

```bash
streamlit run app_demo.py
```

---

## Architecture Deep Dive

For exhaustive theoretical and technical specifications, explore the repository documents:
*   [research_paper.md](research_paper.md) - Theoretical foundations, CLS memory model, and SMT solving invariants.
*   [system_architecture.md](system_architecture.md) - Subsystem interactions, WebAssembly COW sandbox configs, and sequence flows.
*   [specifications.md](specifications.md) - SQL schemas, Neo4j graphs, gRPC proto, and core algorithms.
*   [arcitecture_exp.md](arcitecture_exp.md) - Complete system specification with a full runnable code engine.

---

## Contributing
We welcome contributions! Please check our Contribution Guidelines and join our community.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
