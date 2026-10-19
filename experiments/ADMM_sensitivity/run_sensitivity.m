function run_sensitivity()
here=fileparts(mfilename('fullpath'));cd(here);diary(fullfile(here,'execution.log'));
disp(which('gurobi'));disp(which('sdpvar'));
settings=[1,10,2,.1; .1,10,2,.1; .5,10,2,.1; 2,10,2,.1;10,10,2,.1;1,5,2,.1;1,20,2,.1;1,10,1.5,.1;1,10,3,.1;1,10,2,.05];
results=[];
for i=1:size(settings,1)
 cfg=struct('id',sprintf('A%02d',i),'rho0',settings(i,1),'mu',settings(i,2),'tau',settings(i,3),'eps',settings(i,4));
 try
  r=run_one(cfg);results=[results;r];
  writetable(struct2table(results),fullfile(here,'summary.csv'));
  if i==1 && (r.iterations~=47 || abs(r.primal-.08663)>1e-4 || abs(r.dual-.04004)>1e-4)
   error('Baseline does not reproduce the recorded 47-iteration trajectory. Inspect before continuing.');
  end
 catch err
  fid=fopen(fullfile(here,[cfg.id '_error.txt']),'w');fprintf(fid,'%s',getReport(err,'extended'));fclose(fid);
  if i==1,rethrow(err);end
 end
end
diary off;
end
