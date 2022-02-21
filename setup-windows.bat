conda config --add channels conda-forge
conda config --set channel_priority strict
conda env list
conda create -y -n toast
conda activate toast
conda install python=3 toast
conda install pysm3 libsharp
conda install mpi4py
conda activate toast
python -c 'import toast.tests; toast.tests.run()'
@REM mpirun -np 2 python -c 'import toast.tests; toast.tests.run()'
