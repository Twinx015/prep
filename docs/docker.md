# docker

## containers
docker run -it <image>                    # Run a new container (interactive shell)
docker run -d -p 8080:80 <image>          # Run in the background, map a port
docker run --rm <image>                   # Run and auto-remove the container on exit
docker ps                                 # List running containers
docker ps -a                              # List all containers, including stopped ones
docker stop <container>                   # Stop a running container
docker start <container>                  # Start a stopped container
docker restart <container>                # Restart a container
docker rm <container>                     # Remove a container
docker exec -it <container> bash          # Open a shell inside a running container
docker logs -f <container>                # Follow a container's logs

## images
docker build -t <name>:<tag> .            # Build an image from a Dockerfile
docker images                             # List local images
docker pull <image>                       # Pull an image from a registry
docker push <image>                       # Push an image to a registry
docker rmi <image>                        # Remove a local image
docker tag <image> <new-name>             # Tag an image with a new name/repo

## compose
docker compose up                         # Start services defined in compose.yaml
docker compose up -d                      # Start services in the background
docker compose down                       # Stop and remove services
docker compose logs -f                    # Follow logs for all services
docker compose ps                         # List running compose services
docker compose build                      # Rebuild service images
docker compose restart                    # Restart all services

## volumes & networks
docker volume ls                          # List volumes
docker volume rm <volume>                 # Remove a volume
docker network ls                         # List networks
docker network create <name>              # Create a network

## system
docker system df                          # Show disk usage by images/containers/volumes
docker system prune                       # Remove unused containers, images, and networks
docker system prune -a                    # Also remove unused images (more aggressive)
docker inspect <container>                # Show detailed JSON info about a container/image
