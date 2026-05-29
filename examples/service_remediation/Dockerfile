FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        nginx \
        openssh-server \
        sudo \
        python3 \
        python3-cryptography \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /var/run/sshd

RUN useradd -m -s /bin/bash ansible \
    && echo "ansible ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ansible \
    && chmod 0440 /etc/sudoers.d/ansible \
    && mkdir -p /home/ansible/.ssh \
    && chmod 700 /home/ansible/.ssh

ARG SSH_PUBLIC_KEY
RUN test -n "$SSH_PUBLIC_KEY" \
    && echo "$SSH_PUBLIC_KEY" > /home/ansible/.ssh/authorized_keys \
    && chmod 600 /home/ansible/.ssh/authorized_keys \
    && chown -R ansible:ansible /home/ansible/.ssh

RUN sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

# Remove the default site so the config_drift demo owns nginx's port-80 server block
# without competing with the Debian default. Other demos either don't touch nginx at all
# or start it via the service module (service_remediation), where the default site is fine
# either way.
RUN rm -f /etc/nginx/sites-enabled/default

EXPOSE 22 80

# Start sshd in the foreground as PID 1. Nginx is intentionally NOT started:
# the FSM has to detect it down and start it via the Ansible service module.
CMD ["/usr/sbin/sshd", "-D", "-e"]
