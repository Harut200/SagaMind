# Stage 1: Build dependencies and compile C++ libraries
FROM python:3.10-slim-bullseye AS builder

WORKDIR /build

# Install system utilities needed to build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final minimal execution image
FROM python:3.10-slim-bullseye AS runner

WORKDIR /app

# Install system runtime libraries (specifically for vector embeddings and Z3 binary)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    z3 \
    && rm -rf /var/lib/apt/lists/*

# Copy python packages installed in the builder stage
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

# Copy project files
COPY . .

# Expose FastAPI (8000) and gRPC (50051) ports
EXPOSE 8000
EXPOSE 50051

# Default runtime entrypoint
CMD ["python", "src/main.py"]
