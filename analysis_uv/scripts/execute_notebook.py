"""Запуск всех ячеек с нуля в Python uv; IPython без сервера или обычное ядро Jupyter."""
from pathlib import Path
import argparse
import json
import os
import sys
import tempfile
import time

import nbformat
from nbconvert import HTMLExporter

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--notebook',type=Path,default=ROOT/'VK_full_analysis.ipynb')
    parser.add_argument('--no-html',action='store_true')
    parser.add_argument('--engine',choices=['ipython','jupyter'],default='ipython')
    parser.add_argument('--transport',choices=['ipc','tcp'],
                        default='tcp' if sys.platform=='win32' else 'ipc',
                        help='Локальный IPC на Linux/macOS; TCP loopback на Windows')
    args=parser.parse_args()
    path=args.notebook.resolve()
    nb=nbformat.read(path,as_version=4)
    for cell in nb.cells:
        if cell.cell_type=='code':
            cell.outputs=[]
            cell.execution_count=None
    start=time.perf_counter()
    print(f'Выполняется {path.name}: {sum(c.cell_type=="code" for c in nb.cells)} кодовых ячеек',flush=True)
    os.chdir(ROOT)
    if args.engine=='ipython':
        # Это реальное выполнение всех ячеек, а не заполнение результатов из JSON.
        # Сетевые и Unix-сокеты не нужны. Ядро работает в новом процессе Python.
        with tempfile.TemporaryDirectory(prefix='vk_case_ipython_') as tmp:
            os.environ['IPYTHONDIR']=tmp
            from IPython.core.interactiveshell import InteractiveShell
            from IPython.utils.capture import capture_output
            from traitlets.config import Config
            shell=InteractiveShell.instance(config=Config({'HistoryManager':{'enabled':False}}))
            counter=0
            for cell_index,cell in enumerate(nb.cells):
                if cell.cell_type!='code':
                    continue
                counter+=1
                cell_start=time.perf_counter()
                with capture_output(stdout=True,stderr=True,display=True) as captured:
                    result=shell.run_cell(cell.source,store_history=False)
                outputs=[]
                if captured.stdout:
                    outputs.append(nbformat.v4.new_output('stream',name='stdout',text=captured.stdout))
                if captured.stderr:
                    outputs.append(nbformat.v4.new_output('stream',name='stderr',text=captured.stderr))
                for item in captured.outputs:
                    outputs.append(nbformat.v4.new_output('display_data',data=item.data,metadata=item.metadata))
                cell.outputs=outputs
                cell.execution_count=counter
                cell.metadata['vk_execution']={'engine':'IPython InteractiveShell',
                                            'duration_seconds':round(time.perf_counter()-cell_start,4)}
                error=result.error_before_exec or result.error_in_exec
                if error:
                    cell.outputs.append(nbformat.v4.new_output('error',ename=type(error).__name__,
                                                               evalue=str(error),traceback=[repr(error)]))
                    nbformat.write(nb,ROOT/'analysis'/'failed_execution.ipynb')
                    raise RuntimeError(f'Ошибка в ячейке {cell_index}, кодовой #{counter}: {error}') from error
                print(f'Ячейка {counter}: выполнена',flush=True)
    else:
        from nbclient import NotebookClient
        from jupyter_client import KernelManager
        from jupyter_client.kernelspec import KernelSpecManager
        with tempfile.TemporaryDirectory(prefix='vk_case_kernel_') as tmp:
            kernels=Path(tmp)/'kernels'
            spec=kernels/'vk-case-analysis'; spec.mkdir(parents=True)
            (spec/'kernel.json').write_text(json.dumps({'argv':[sys.executable,'-m','ipykernel_launcher','-f','{connection_file}'],
                'display_name':'Python (VK case uv)','language':'python',
                'env':{'MPLBACKEND':'Agg','OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1'}}),encoding='utf-8')
            km=KernelManager(kernel_name='vk-case-analysis',
                             kernel_spec_manager=KernelSpecManager(kernel_dirs=[str(kernels)]),
                             transport=args.transport,
                             ip=str(Path(tmp)/'kernel') if args.transport=='ipc' else '127.0.0.1')
            client=NotebookClient(nb,km=km,kernel_name='vk-case-analysis',timeout=900,allow_errors=False,
                                 resources={'metadata':{'path':str(ROOT)}},record_timing=True,store_widget_state=False)
            try:
                client.execute()
            except Exception:
                nbformat.write(nb,ROOT/'analysis'/'failed_execution.ipynb')
                raise
            finally:
                if km.has_kernel:
                    km.shutdown_kernel(now=True)
    nb.metadata.kernelspec={'display_name':'Python 3 (uv project)','language':'python','name':'python3'}
    nb.metadata['execution_engine']=args.engine
    nbformat.validate(nb)
    errors=[output for cell in nb.cells if cell.cell_type=='code' for output in cell.get('outputs',[]) if output.output_type=='error']
    if errors:raise RuntimeError('В выполненном блокноте присутствуют ошибки')
    nbformat.write(nb,path)
    (ROOT/'metadata/execution.json').write_text(json.dumps({
        'engine':args.engine,'python':sys.version.split()[0],
        'code_cells':sum(c.cell_type=='code' for c in nb.cells),
        'error_outputs':len(errors),'fresh_process':True,
        'source_file':path.name,
        'note':'Все ячейки выполнены последовательно; результаты получены вычислениями. Режим ipython не проверяет сетевой запуск Jupyter Server.'
    },ensure_ascii=False,indent=2),encoding='utf-8')
    if not args.no_html:
        exporter=HTMLExporter()
        exporter.exclude_input_prompt=True
        exporter.exclude_output_prompt=True
        body,_=exporter.from_notebook_node(nb)
        path.with_suffix('.html').write_text(body,encoding='utf-8')
    print(f'Готово за {time.perf_counter()-start:.1f} с. Ошибок ячеек: {len(errors)}',flush=True)

if __name__=='__main__':
    main()
