import os
import time
import docker

from pathlib import Path


def init(repo_path=None, outputs_path="outputs"):
	client = docker.from_env()

	sandbox_name = "kali-linux"

	volumes = {}
	if outputs_path:
		out_dir = Path(outputs_path).resolve()
		out_dir.mkdir(parents=True, exist_ok=True)
		volumes[str(out_dir)] = {'bind': '/outputs', 'mode': 'rw'}
	if repo_path:
		repo_dir = Path(repo_path).resolve()
		if repo_dir.exists():
			volumes[str(repo_dir)] = {'bind': '/data', 'mode': 'rw'}

	found = client.containers.list(filters={'name': sandbox_name}, all=True)

	if found:
		container = found[0]
		binds = container.attrs.get('HostConfig', {}).get('Binds') or []
		if volumes and not binds:
			try:
				container.stop()
			except Exception:
				pass
			container.remove()
			found = []

	if not found:
		# Check or build image
		image = "vulnalysis:latest"
		try:
			client.images.get(image)
		except Exception:
			if os.path.exists("docker/Dockerfile"):
				client.images.build(path="docker", tag=image, rm=True)
			elif os.path.exists("Dockerfile"):
				client.images.build(path=".", tag=image, rm=True)
			else:
				client.images.pull("kalilinux/kali-rolling")
				image = "kalilinux/kali-rolling"

		container = client.containers.run(
			image,
			detach=True,
			tty=True,
			name=sandbox_name,
			cap_add=['NET_ADMIN', 'SYS_PTRACE'],
			devices=["/dev/net/tun"],
			environment={"DEBIAN_FRONTEND": "noninteractive"},
			volumes=volumes,
			stdin_open=True
		)

		# Set up container
		if image == "kalilinux/kali-rolling":
			commands = (
				'apt update && '
				'apt -y install curl'
			)
			result = container.exec_run(f'/bin/bash -c "{commands}"', stdout=True, stderr=True)

			check = container.exec_run('which curl')

		container.stop()

	if container.status != 'running':
		container.start()

		# Start TUN if needed
		container.exec_run('/bin/bash -c "mkdir -p /dev/net && mknod /dev/net/tun c 10 200 && chmod 600 /dev/net/tun"')
		
		# Start VPN
		container.exec_run('bash -c "openvpn /data/*.ovpn"', detach=True)
		time.sleep(3)

	return container

if __name__ == '__main__':
	container = init()
	print(f"Container '{container.name}' is {container.status}")