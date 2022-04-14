# Installation

Clone this repository.

Make sure you have `venv` installed

~~~ sh
python3 -m pip install venv
~~~

Create a virtual environment
~~~
python3 -m venv venv
~~~

Activate the virtual environment

~~~
source venv/bin/activate
~~~

Install the requirements

~~~
python3 -m pip install -r requirements.txt
~~~

Create the configuration file
~~~
mv settings_example.conf settings.conf
chmod 600 settings.conf
~~~

and fill in the values.