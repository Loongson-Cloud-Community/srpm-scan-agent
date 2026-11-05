FROM lcr.loongnix.cn/loongnix-server/loongnix-server:23.3
RUN sed -i '/\[updates\]/,/enabled=/ s/enabled=1/enabled=0/' /etc/yum.repos.d/Loongnix-Server.repo

ARG JENKINS_AGENT_JAR_URL

ENV PATH="/usr/lib/jvm/jdk-25.0.4/bin:${PATH}" \
    HOME=/home/jenkins \
    JENKINS_AGENT_WORKDIR=/home/jenkins/agent

USER root

RUN dnf install -y \
      ca-certificates \
      coreutils \
      curl \
      gzip \
      python3 \
      tar \
      util-linux \
      xz && \
    dnf clean all

RUN mkdir -p /usr/lib/jvm/jdk-25.0.4 && \
    curl --fail --location --retry 5 \
      --output /tmp/jdk25.tar.gz \
      "https://ftp.loongnix.cn/Java/openjdk25/loongson25.5.22-fx-jdk25.0.4_7-linux-loongarch64-glibc2.34.tar.gz" && \
    tar -xzf /tmp/jdk25.tar.gz -C /usr/lib/jvm/jdk-25.0.4 --strip-components=1 && \
    rm -f /tmp/jdk25.tar.gz && \
    mkdir -p /usr/share/jenkins && \
    curl --fail --location --retry 5 \
      --output /usr/share/jenkins/agent.jar \
      "${JENKINS_AGENT_JAR_URL}"

ARG AGENT_UID=1000
ARG AGENT_GID=1000
RUN groupadd --gid ${AGENT_GID} jenkins && \
    useradd --uid ${AGENT_UID} --gid ${AGENT_GID} --create-home --shell /bin/bash jenkins && \
    mkdir -p /home/jenkins/agent && \
    chown -R jenkins:jenkins /home/jenkins

COPY scripts/run-srpm-scan /usr/local/bin/run-srpm-scan
COPY scripts/send-srpm-event /usr/local/bin/send-srpm-event
COPY scripts/jenkins-agent-entrypoint /usr/local/bin/jenkins-agent-entrypoint

RUN chmod 0755 \
      /usr/local/bin/run-srpm-scan \
      /usr/local/bin/send-srpm-event \
      /usr/local/bin/jenkins-agent-entrypoint

USER jenkins
WORKDIR /home/jenkins/agent

ENTRYPOINT ["jenkins-agent-entrypoint"]
