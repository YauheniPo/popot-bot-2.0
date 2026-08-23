FROM codercom/code-server:latest

# Install additional useful packages
USER root
RUN apt-get update && apt-get install -y \
    openssh-client \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Switch back to coder user
USER coder

# Copy configuration
COPY config.json /etc/code-server/config.json

EXPOSE 8080

CMD ["code-server", "--bind-address", "0.0.0.0", "--auth", "password", "/home/coder/project"]