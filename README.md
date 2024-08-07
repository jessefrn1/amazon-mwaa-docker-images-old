## aws-mwaa-docker-images

## Overview

This repository contains the Docker Images that [Amazon
MWAA](https://aws.amazon.com/managed-workflows-for-apache-airflow/) will use in future versions of
Airflow. Eventually, we will deprecate
[aws-mwaa-local-runner](https://github.com/aws/aws-mwaa-local-runner) in favour of this package.
However, at this point, this repository is still under development.

## Using the Airflow Image

Currently, Airflow v2.9.2 is supported. Future versions in parity with Amazon MWAA will be added.

To experiment with the image using a vanilla Docker setup, follow these steps:

0. Ensure you have:
   - Python 3.11 or later.
   - Docker and Docker Compose.
1. Clone this repository.
2. This repository makes use of Python virtual environments. To create them, from the root of the
   package, execute the following command:

```
python3 create_venvs.py
```

3. Build the `fake-statsd-server` image:
```
cd <amazon-mwaa-docker-images path>/images/fake-statsd-server
./build.sh
```

4. Build the Airflow v2.9.2 Docker image using:

```
cd <amazon-mwaa-docker-images path>/images/airflow/2.9.2
./run.sh
```

Airflow should be up and running now. You can access the web server on your localhost on port 8080.

Optionally, if you also want to publish the image to ECR, you can execute the following command:

```
./push_to_ecr.sh
```

Notice that this command requires you to have valid AWS credentials in your `~/.aws/credentials`
file or environment variables, and that the credentials you use have the necessary permissions to
create an ECR repository and push a Docker image.

### Note on the Generated Docker Images

When you build the Docker images of a certain Airflow version, using either `build.sh` or `run.sh`
(which automatically also calls `build.sh` for you), multiple Docker images will actually be
generated. For example, for Airflow 2.9 (the only currently supported version), you will notice the
following images:

| Repository                        | Tag                           |
| --------------------------------- | ----------------------------- |
| amazon-mwaa-docker-images/airflow | 2.9.2                         |
| amazon-mwaa-docker-images/airflow | 2.9.2-dev                     |
| amazon-mwaa-docker-images/airflow | 2.9.2-explorer                |
| amazon-mwaa-docker-images/airflow | 2.9.2-explorer-dev            |
| amazon-mwaa-docker-images/airflow | 2.9.2-explorer-privileged     |
| amazon-mwaa-docker-images/airflow | 2.9.2-explorer-privileged-dev |

Each of the postfixes added to the image tag represents a certain build type, as explained below:

- `explorer`: The 'explorer' build type is almost identical to the default build type except that it
  doesn't include an entrypoint, meaning that if you run this image locally, it will not actually
  start Airflow. This is useful for debugging purposes to run the image and look around its content
  without starting airflow. For example, you might want to explore the file system and see what is
  available where.
- `privileged`: Privileged images are the same as their non-privileged counterpart except that they
  run as the `root` user instead. This gives the user of this Docker image
  elevated permissions. This can be useful if the user wants to do some experiments as the root
  user, e.g. installing DNF packages, creating new folders outside the airflow user folder, among
  others.
- `dev`: These images have extra packages installed for debugging purposes. For example, typically
  you wouldn't want to install a text editor in a Docker image that you use for production. However,
  during debugging, you might want to open some files and inspect their contents, make some changes,
  etc. Thus, we install an editor in the dev images to aid with such use cases. Similarly, we
  install tools like `wget` to make it possible for the user to fetch web pages. For a complete
  listing of what is installed in `dev` images, see the `bootstrap-dev` folders.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.
