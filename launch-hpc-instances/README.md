# Launch HPC instances

Launch compute instances on the Catalyst Cloud with placement optimised to
increase CPU performance.

## How to use

### Preparing your cloud project

Import an SSH public key on the cloud, so it can be injected into the compute
instances for SSH access later:
http://docs.catalystcloud.io/first-instance/dashboard.html#uploading-an-ssh-key

Create a private network on all regions of the Catalyst Cloud. Name the network
the same in all regions. Please note that cloud tenants already come with a
network called "private-net" pre-configured in the Porirua region, so we
recommend creating the same in Hamilton and Wellington. Instructions on how to
create and configure new networks can be found here:
http://docs.catalystcloud.io/network.html#adding-a-network

If you are launching more than 10 compute instances, you need to ask Catalyst to
increase your quota for server_group_members. You can create a support request
to ask for quota changes:
https://dashboard.cloud.catalyst.net.nz/management/tickets/

### Preparing your local environment

Create a python virtual environment and install the libraries required by the
command line tool in it.

``` bash
virtualenv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Create the compute instances

Source an openrc file with your credentials to access the Catalyst Cloud, as
described at
http://docs.catalystcloud.io/getting-started/cli.html#source-an-openstack-rc-file.

> Note: if you do not source an openrc file, you will need to pass the
> authentication information to the command line tool when running it. See
> ./launch-hpc-instances.py help for more infromation.

Make sure your python virtual environment is activated (`source
venv/bin/activate`).

Find out the name of the compute flavour that matches the amount of CPU and RAM
you would like for each compute instance
(https://catalyst.net.nz/catalyst-cloud/prices).

Sample usage:

``` bash
./launch-hpc-instances.py create --instance-count 10 \
                                 --name-prefix hpc \
                                 --keypair-name keypair \
                                 --image-name ubuntu-16.04-x86_64 \
                                 --flavor-name c1.c4r8 \
                                 --volume-size 50 \
                                 --assign-public-ip
```

If you need to pre-install some software on your instances, you can use the
`--path-to-cloud-init-script` flag to pass a script that will be executed as
root on the first boot of the instance.

Check the help to find out more information on how to use it:
./launch.hpc-instances.py help create
